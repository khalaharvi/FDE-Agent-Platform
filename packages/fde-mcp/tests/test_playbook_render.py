"""The playbook renderer: byte-stability, and the shapes a workflow can take.

The golden-file test is the point of this module. A playbook is a document
derived from a sealed commit, so the same workflow must render the same bytes
on every database that holds it -- otherwise "export the playbook" produces a
file that diffs against itself after a rebuild, and nobody can tell a real
change from a re-render.

Everything the golden fixture needs is seeded here rather than taken from
`db/seed_demo.sql`, because CI rebuilds without `--with-demo` and a test that
silently skips is a test that stops holding. The graph it seeds is
deliberately the demo engagement's content -- the same six nodes, the same
edges -- so the fixture reads like a playbook over the demo data while still
running anywhere.

The unit tests below take no database at all: `render_playbook` is a pure
function over rows, so the edge cases worth pinning (a step citing nothing, a
decision's branches, a workflow that is all human steps) are cheaper and
clearer as literals than as fixtures.
"""

from __future__ import annotations

import os
import re
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Any

import psycopg
import pytest
import pytest_asyncio
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from fde_mcp import db
from fde_mcp.config import get_settings
from fde_mcp.playbook import render_playbook
from fde_mcp.tools import workflow

if TYPE_CHECKING:
    from collections.abc import Iterator

GOLDEN = Path(__file__).parent / "fixtures" / "playbook_golden.md"

# Fixed so the fixture cannot move when the suite is collected differently:
# both conftests `setdefault` FDE_AGENT_RUNTIME_ARN, so whichever is imported
# first would otherwise decide what `wf_draft` records as `authored_by`.
GOLDEN_AUTHOR = "arn:aws:bedrock-agentcore:us-west-2:123456789012:runtime/fde-workflow"
GOLDEN_PUBLISHER = "owner@example.com"
GOLDEN_SLUG = "q2c-discount-approval"

# The demo engagement's graph, seeded under this module's own engagement.
# `role.deal_desk` carries `is_role_title` because `node_role_is_not_a_person`
# refuses a role node without it -- the schema's way of stopping a named
# individual being modelled as a role.
GOLDEN_NODES: list[tuple[str, str, str, str, dict[str, Any]]] = [
    (
        "proc.quote_to_cash",
        "process",
        "Quote to Cash",
        "End-to-end from quote creation to booked revenue.",
        {},
    ),
    ("act.create_quote", "activity", "Create Quote", "Sales rep builds a quote in CPQ.", {}),
    (
        "act.discount_review",
        "activity",
        "Discount Review",
        "Deal desk reviews quotes discounted beyond policy.",
        {},
    ),
    (
        "ctl.discount_threshold_20",
        "control",
        "20% Discount Threshold",
        "Quotes above 20% discount require deal desk approval before send.",
        {"data_classification": "internal"},
    ),
    ("sys.cpq", "system", "CPQ", "Configure-price-quote system of record for quotes.", {}),
    (
        "role.deal_desk",
        "role",
        "Deal Desk Analyst",
        "Reviews and approves non-standard pricing.",
        {"is_role_title": True},
    ),
]

GOLDEN_EDGES: list[tuple[str, str, str]] = [
    ("act.create_quote", "belongs_to", "proc.quote_to_cash"),
    ("act.discount_review", "belongs_to", "proc.quote_to_cash"),
    ("act.create_quote", "precedes", "act.discount_review"),
    ("act.create_quote", "gated_by", "ctl.discount_threshold_20"),
    ("act.discount_review", "depends_on", "sys.cpq"),
    ("act.discount_review", "recorded_in", "sys.cpq"),
    ("role.deal_desk", "performs", "act.discount_review"),
]


def _golden_steps() -> list[workflow.StepSpec]:
    """One step of every kind the renderer formats differently.

    The first step's bindings are fed in `enforces`-then-`implements` order on
    purpose: the renderer sorts them the other way round, so a fixture that
    handed them over already sorted would pass whether or not the sort
    survived.
    """
    return [
        workflow.StepSpec(
            step_key="check_discount",
            ordinal=1,
            kind="tool",
            title="Look up the quote's discount",
            instruction=(
                "Read the quote's discount percentage from CPQ. If it is at or below "
                "20%, the quote can be sent without deal-desk involvement."
            ),
            tool_name="cpq_discount_check",
            bindings=[
                workflow.StepBindingSpec(
                    subject_kind="node",
                    subject_key="ctl.discount_threshold_20",
                    relation="enforces",
                ),
                workflow.StepBindingSpec(
                    subject_kind="node", subject_key="act.create_quote", relation="implements"
                ),
            ],
        ),
        workflow.StepSpec(
            step_key="deal_desk_review",
            ordinal=2,
            kind="human",
            title="Deal desk reviews the discount",
            instruction=(
                "Open the quote in CPQ and weigh the requested discount against the "
                "account's history and the current quarter's pricing guidance."
            ),
            human_prompt="Approve this discount, or send it back with a reason?",
            human_schema={"type": "object", "properties": {"approved": {"type": "boolean"}}},
            requires_human=True,
            on_failure="escalate",
            bindings=[
                workflow.StepBindingSpec(
                    subject_kind="node", subject_key="act.discount_review", relation="implements"
                ),
                workflow.StepBindingSpec(
                    subject_kind="node", subject_key="sys.cpq", relation="depends_on"
                ),
            ],
        ),
        workflow.StepSpec(
            step_key="record_outcome",
            ordinal=3,
            kind="sor_write",
            title="Record the decision in CPQ",
            instruction="Write the deal desk's decision back onto the quote record.",
            sor_adapter_key="cpq",
            sor_write_op="update_quote_status",
            bindings=[
                workflow.StepBindingSpec(
                    subject_kind="node", subject_key="sys.cpq", relation="records_to"
                )
            ],
        ),
        workflow.StepSpec(
            step_key="notify_rep",
            ordinal=4,
            kind="notify",
            title="Tell the rep the outcome",
            instruction="Notify the quote's owner that the discount review has closed.",
        ),
    ]


# ---------------------------------------------------------------------------
# Golden file
# ---------------------------------------------------------------------------


@pytest.fixture
def _pinned_author(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("FDE_AGENT_RUNTIME_ARN", GOLDEN_AUTHOR)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _connect() -> psycopg.Connection[dict[str, Any]]:
    return psycopg.connect(os.environ["FDE_DB_DSN"], autocommit=True, row_factory=dict_row)


@pytest.fixture
def golden_graph() -> tuple[str, int]:
    """The demo engagement's six nodes and their edges, under a fresh engagement.

    Written as the calling OS role, the same shortcut `conftest.seed` takes:
    this stands in for a merge that already happened, and routing it through
    `hitl.merge_proposal` would be testing the fixture.
    """
    engagement_id = str(uuid.uuid4())
    with _connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO kg.commit (engagement_id, status, title, authored_by, sealed_by,
                                   sealed_at, content_digest)
            VALUES (%(eng)s, 'sealed', 'playbook golden fixture', 'pytest', 'pytest', now(),
                    encode(digest('playbook golden fixture', 'sha256'), 'hex'))
            RETURNING commit_id
            """,
            {"eng": engagement_id},
        )
        commit = cur.fetchone()
        assert commit is not None
        commit_id = commit["commit_id"]

        for node_key, node_type, label, summary, attributes in GOLDEN_NODES:
            cur.execute(
                """
                INSERT INTO kg.node (engagement_id, node_key, node_type, label, summary,
                                     attributes, commit_id)
                VALUES (%(eng)s, %(key)s, %(ntype)s, %(label)s, %(summary)s, %(attrs)s, %(cid)s)
                """,
                {
                    "eng": engagement_id,
                    "key": node_key,
                    "ntype": node_type,
                    "label": label,
                    "summary": summary,
                    "attrs": Jsonb(attributes),
                    "cid": commit_id,
                },
            )
        for src, edge_type, dst in GOLDEN_EDGES:
            cur.execute(
                """
                INSERT INTO kg.edge (engagement_id, edge_key, src_key, dst_key, edge_type,
                                     commit_id, human_confirmed)
                VALUES (%(eng)s, kg.make_edge_key(%(src)s, %(etype)s, %(dst)s), %(src)s,
                        %(dst)s, %(etype)s, %(cid)s, true)
                """,
                {
                    "eng": engagement_id,
                    "src": src,
                    "dst": dst,
                    "etype": edge_type,
                    "cid": commit_id,
                },
            )
    return engagement_id, commit_id


@pytest_asyncio.fixture
async def golden_workflow_id(_pinned_author: None, golden_graph: tuple[str, int]) -> int:
    """A published workflow over that graph, authored the way an agent authors one.

    `wf_draft` is the real authoring path -- it resolves each binding's
    `pinned_label` from the live graph and runs `wf.assert_faithful` -- and
    `wf.publish_workflow` is the only publish path there is. Neither is
    reimplemented here, so the fixture is what an agent and a reviewer
    produce between them, not an approximation of it.
    """
    engagement_id, commit_id = golden_graph
    drafted = await workflow.wf_draft(
        engagement_id=engagement_id,
        slug=GOLDEN_SLUG,
        title="Q2C discount approval",
        root_process_key="proc.quote_to_cash",
        pinned_commit_id=commit_id,
        steps=_golden_steps(),
    )
    assert "error" not in drafted, drafted
    workflow_id = int(drafted["workflow_id"])

    with _connect() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT workflow_id FROM wf.publish_workflow(%(wid)s, %(by)s)",
            {"wid": workflow_id, "by": GOLDEN_PUBLISHER},
        )
    return workflow_id


@pytest.mark.requires_db
async def test_exported_playbook_matches_the_golden_file(
    golden_workflow_id: int, tmp_path: Path
) -> None:
    result = await workflow.wf_export_playbook(golden_workflow_id)
    assert "error" not in result, result
    actual = str(result["markdown"])

    if not GOLDEN.exists() or actual != GOLDEN.read_text(encoding="utf-8"):
        written = tmp_path / GOLDEN.name
        written.write_text(actual, encoding="utf-8")
        pytest.fail(
            f"rendered playbook does not match {GOLDEN}. The rendered document was "
            f"written to {written}; diff it, and if the change is intended copy it "
            f"over the fixture."
        )


@pytest.mark.requires_db
async def test_export_carries_nothing_that_varies_between_databases(
    golden_workflow_id: int,
) -> None:
    """The determinism rule, asserted rather than left to the fixture to imply.

    A golden file passes as long as nobody rebuilds the database. These
    assertions are what fail on the change that would have broken it -- an id
    or a timestamp finding its way into the document.
    """
    result = await workflow.wf_export_playbook(golden_workflow_id)
    markdown = str(result["markdown"])

    async with db.tool_transaction() as conn, conn.cursor() as cur:
        await cur.execute(
            "SELECT engagement_id::text AS eng, workflow_uuid::text AS wf_uuid, "
            "pinned_commit_id FROM wf.workflow WHERE workflow_id = %(wid)s",
            {"wid": golden_workflow_id},
        )
        row = await cur.fetchone()
    assert row is not None

    assert row["eng"] not in markdown, "the engagement uuid differs per database"
    assert row["wf_uuid"] not in markdown, "the workflow uuid differs per database"
    assert not re.search(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:", markdown), (
        "a timestamp in the document makes every export differ from the last"
    )
    assert f"commit {row['pinned_commit_id']}" not in markdown, (
        "commit ids are serials; the document names the content digest instead"
    )


@pytest.mark.requires_db
async def test_export_is_identical_on_repeated_calls(golden_workflow_id: int) -> None:
    first = await workflow.wf_export_playbook(golden_workflow_id)
    second = await workflow.wf_export_playbook(golden_workflow_id)
    assert first["markdown"] == second["markdown"]
    assert first["slug"] == GOLDEN_SLUG
    assert first["status"] == "published"
    assert first["version"] == 1


@pytest.mark.requires_db
async def test_export_of_a_missing_workflow_says_so() -> None:
    result = await workflow.wf_export_playbook(999_999_999)
    assert result["error"] == "workflow not found"
    assert result["hint"] == "check workflow_id"


# ---------------------------------------------------------------------------
# The renderer itself -- no database
# ---------------------------------------------------------------------------

_MINIMAL_WORKFLOW: dict[str, Any] = {
    "title": "Minimal",
    "slug": "minimal",
    "version": 2,
    "status": "published",
    "root_process_key": "proc.minimal",
    "autonomy_level": "assisted",
    "runnable_by": [],
    "pinned_digest": "d" * 64,
    "authored_by": "agent:workflow",
    "published_by": "owner@example.com",
    "description": None,
}


def _step(**overrides: Any) -> dict[str, Any]:
    step: dict[str, Any] = {
        "step_key": "only_step",
        "ordinal": 1,
        "kind": "human",
        "title": "Do the thing",
        "instruction": "Do it.",
        "tool_name": None,
        "human_prompt": None,
        "branches": None,
        "sor_adapter_key": None,
        "sor_write_op": None,
        "requires_human": True,
        "on_failure": "halt",
    }
    step.update(overrides)
    return step


def test_a_step_citing_nothing_says_what_that_costs() -> None:
    rendered = render_playbook(_MINIMAL_WORKFLOW, [_step()], {}, [])
    assert "**No grounding recorded.**" in rendered
    assert "cannot be published" in rendered


def test_a_notify_step_citing_nothing_is_not_an_error() -> None:
    rendered = render_playbook(_MINIMAL_WORKFLOW, [_step(kind="notify")], {}, [])
    assert "exempt from the faithfulness check" in rendered
    assert "No grounding recorded" not in rendered


def test_bindings_render_sorted_with_their_pinned_labels() -> None:
    bindings = {
        "only_step": [
            {
                "relation": "enforces",
                "subject_kind": "edge",
                "subject_key": "edge.a",
                "pinned_label": None,
            },
            {
                "relation": "implements",
                "subject_kind": "node",
                "subject_key": "act.b",
                "pinned_label": "Bee",
            },
        ]
    }
    rendered = render_playbook(_MINIMAL_WORKFLOW, [_step()], bindings, [])
    enforces = rendered.index("`enforces` edge `edge.a`")
    implements = rendered.index("`implements` node `act.b` — Bee")
    assert implements < enforces, (
        "bindings sort by the schema's relation order -- what a step implements "
        "before what it enforces -- not by insertion order"
    )


def test_decision_branches_render_in_evaluation_order() -> None:
    branches = [
        {"when": "$.check_discount.discount_pct > 20", "goto": "deal_desk_review"},
        {"else": "notify_rep"},
    ]
    rendered = render_playbook(
        _MINIMAL_WORKFLOW, [_step(kind="decision", branches=branches)], {}, []
    )
    assert "**Branches, evaluated in order:**" in rendered
    assert "1. When `$.check_discount.discount_pct > 20`, go to step `deal_desk_review`" in rendered
    assert "2. Otherwise, go to step `notify_rep`" in rendered


def test_a_decision_with_no_branches_names_the_consequence() -> None:
    rendered = render_playbook(_MINIMAL_WORKFLOW, [_step(kind="decision")], {}, [])
    assert "**No branches are defined.**" in rendered
    assert "halts the run when it is reached" in rendered


def test_a_branch_the_runner_cannot_evaluate_is_shown_not_hidden() -> None:
    """A malformed branch is exactly what a reviewer needs to see."""
    rendered = render_playbook(
        _MINIMAL_WORKFLOW,
        [_step(kind="decision", branches={"approved": "next_step"})],
        {},
        [],
    )
    assert '"approved": "next_step"' in rendered


def test_a_human_only_workflow_reads_as_a_procedure() -> None:
    steps = [
        _step(step_key="ask_one", ordinal=1, human_prompt="Is the customer on contract?"),
        _step(
            step_key="ask_two",
            ordinal=2,
            title="Confirm the renewal date",
            instruction="Check the renewal date against the CRM.",
            human_prompt="Does the renewal date match?",
        ),
    ]
    bindings = {
        key: [
            {
                "relation": "implements",
                "subject_kind": "node",
                "subject_key": "act.ask",
                "pinned_label": "Ask",
            }
        ]
        for key in ("ask_one", "ask_two")
    }
    rendered = render_playbook(_MINIMAL_WORKFLOW, steps, bindings, [])
    assert "### 1. Do the thing" in rendered
    assert "### 2. Confirm the renewal date" in rendered
    assert rendered.count("- Requires a human: yes") == 2
    assert "**Ask the operator:** Is the customer on contract?" in rendered
    assert "No grounding recorded" not in rendered


def test_runnable_by_names_the_groups_or_says_anyone() -> None:
    anyone = render_playbook(_MINIMAL_WORKFLOW, [_step()], {}, [])
    assert "Runnable by: anyone in the product-operations group." in anyone

    restricted = {**_MINIMAL_WORKFLOW, "runnable_by": ["deal-desk", "revops"]}
    rendered = render_playbook(restricted, [_step()], {}, [])
    assert "Runnable by: `deal-desk`, `revops`." in rendered
    assert 'runnable_by: ["deal-desk", "revops"]' in rendered


def test_an_empty_process_walk_says_so_instead_of_showing_nothing() -> None:
    rendered = render_playbook(_MINIMAL_WORKFLOW, [_step()], {}, [])
    assert "The graph records no activities under `proc.minimal`" in rendered


def test_the_process_walk_is_labelled_as_current_not_pinned() -> None:
    flow = [
        {
            "ordinal": 1,
            "activity_key": "act.one",
            "label": "One",
            "performed_by": ["role.b", "role.a"],
            "gated_by": [],
            "records_to": [],
            "next_keys": ["act.two"],
        }
    ]
    rendered = render_playbook(_MINIMAL_WORKFLOW, [_step()], {}, flow)
    assert "the graph as it stands today" in rendered
    assert "1. **One** (`act.one`)" in rendered
    assert "- Performed by: `role.a`, `role.b`" in rendered, "key lists sort"
    assert "Gated by" not in rendered, "an empty edge set is omitted, not shown as a blank"


def test_a_draft_carries_a_banner_and_a_published_workflow_does_not() -> None:
    draft = {**_MINIMAL_WORKFLOW, "status": "draft", "published_by": None}
    rendered = render_playbook(draft, [_step()], {}, [])
    assert "**This workflow is draft, not published.**" in rendered
    assert "published_by" not in rendered

    published = render_playbook(_MINIMAL_WORKFLOW, [_step()], {}, [])
    assert "not published" not in published
    assert "checked at publication" in published


def test_a_missing_digest_is_stated_rather_than_left_blank() -> None:
    unpinned = {**_MINIMAL_WORKFLOW, "pinned_digest": None}
    rendered = render_playbook(unpinned, [_step()], {}, [])
    assert "pinned_commit_digest: null" in rendered
    assert "carries no content digest" in rendered


def test_front_matter_survives_a_title_that_would_break_yaml() -> None:
    hostile = {**_MINIMAL_WORKFLOW, "title": 'Discount: over 20% -- "urgent"'}
    rendered = render_playbook(hostile, [_step()], {}, [])
    assert 'title: "Discount: over 20% -- \\"urgent\\""' in rendered


def test_authored_text_is_normalised_so_invisible_bytes_do_not_change_the_output() -> None:
    windows = render_playbook(
        _MINIMAL_WORKFLOW, [_step(instruction="Line one.  \r\nLine two.")], {}, []
    )
    unix = render_playbook(_MINIMAL_WORKFLOW, [_step(instruction="Line one.\nLine two.")], {}, [])
    assert windows == unix


def test_output_ends_in_exactly_one_newline() -> None:
    rendered = render_playbook(_MINIMAL_WORKFLOW, [_step()], {}, [])
    assert rendered.endswith("\n")
    assert not rendered.endswith("\n\n")
