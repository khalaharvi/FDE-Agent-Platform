"""The three surfaces that render a workflow, and the one document behind them.

The console page, `GET /api/workflows/{id}/playbook.md` and the
`wf_export_playbook` MCP tool are three ways to read one workflow. The test
that matters most here is the last one: the gate's queries and the MCP tool's
queries are separate SQL in separate packages, and the only thing keeping
them from drifting into two subtly different documents is a test that renders
through both and diffs the bytes.
"""

from __future__ import annotations

import uuid
from http import HTTPStatus
from typing import Any

import pytest
from gate_seed import BOUND_NODE_KEYS, OWNER, SME

from fde_gate import ui
from fde_gate.handler import build_router
from fde_gate.http import Request
from fde_gate.service import workflows
from fde_mcp.tools import workflow as mcp_workflow

pytestmark = pytest.mark.requires_db

# An instruction that is markup. Step instructions are agent-authored from
# customer documents, exactly as proposal payloads are (see test_ui.py).
HOSTILE_INSTRUCTION = '<script>alert("xss")</script>'

STEPS: list[dict[str, Any]] = [
    {
        "step_key": "pull_quote",
        "kind": "tool",
        "title": "Pull the quote from CPQ",
        "instruction": "Read the quote record.",
        "tool_name": "cpq_get_quote",
    },
    {
        "step_key": "deal_desk",
        "kind": "human",
        "title": "Deal desk decides",
        "instruction": "Weigh the discount against the account history.",
        "human_prompt": "Approve this discount?",
        "human_schema": {"type": "object"},
        "requires_human": True,
        "on_failure": "escalate",
    },
]


def _get(path: str, workflow_id: int, principal: str = SME) -> Request:
    return Request(
        method="GET",
        path=path,
        path_params={"workflow_id": str(workflow_id)},
        principal=principal,
    )


# ---------------------------------------------------------------------------
# The console page
# ---------------------------------------------------------------------------


async def test_workflow_page_shows_the_steps_and_their_grounding(make_workflow: Any) -> None:
    workflow_id = make_workflow(STEPS)
    page = await ui.workflow_page(_get(f"/ui/workflows/{workflow_id}", workflow_id))

    assert page.status == HTTPStatus.OK
    assert page.content_type.startswith("text/html")
    assert isinstance(page.body, str)
    text = " ".join(page.body.split())

    # The two questions a reviewer opens this page with: what does it tell the
    # operator to do, and what says the business actually does it.
    assert "Pull the quote from CPQ" in text
    assert "Weigh the discount against the account history." in text
    assert "Approve this discount?" in text
    assert "requires a human" in text
    assert "on failure: escalate" in text
    assert "sys.cpq" in text, "the bound graph element must be on the page"
    assert "implements" in text

    assert f"/api/workflows/{workflow_id}/playbook.md" in text, "the export must be reachable"


async def test_workflow_page_escapes_agent_authored_markup(make_workflow: Any) -> None:
    workflow_id = make_workflow(
        [{"step_key": "hostile", "kind": "tool", "instruction": HOSTILE_INSTRUCTION}]
    )
    page = await ui.workflow_page(_get(f"/ui/workflows/{workflow_id}", workflow_id))

    assert isinstance(page.body, str)
    assert "alert" in page.body, "the instruction reached the page..."
    assert HOSTILE_INSTRUCTION not in page.body, "...but not as executable markup"
    assert "<script>" not in page.body


async def test_a_missing_workflow_renders_a_page_not_a_stack_trace() -> None:
    """Same shape as `proposal_page`: a page saying what is missing."""
    page = await ui.workflow_page(_get("/ui/workflows/999999999", 999_999_999))
    assert page.status == HTTPStatus.OK
    assert isinstance(page.body, str)
    assert "There is no workflow 999999999 here." in page.body


async def test_an_unbound_step_says_publishing_will_be_refused(make_workflow: Any) -> None:
    workflow_id = make_workflow(STEPS, publish=False, bind=False)
    page = await ui.workflow_page(_get(f"/ui/workflows/{workflow_id}", workflow_id))

    assert isinstance(page.body, str)
    text = " ".join(page.body.split())
    assert "No grounding recorded." in text
    assert "cannot be published" in text
    assert "Publish this workflow" in text, "the publish button lives on the detail page"


async def test_a_refused_publish_lands_back_on_the_workflow_with_the_reason(
    make_workflow: Any,
) -> None:
    """The unbound step keys and the steps themselves belong on one screen."""
    workflow_id = make_workflow(STEPS, publish=False, bind=False)
    response = await ui.publish_post(
        Request(
            method="POST",
            path=f"/ui/workflows/{workflow_id}/publish",
            path_params={"workflow_id": str(workflow_id)},
            principal=OWNER,
        )
    )
    assert response.status == HTTPStatus.SEE_OTHER
    location = response.headers["Location"]
    assert location.startswith(f"/ui/workflows/{workflow_id}?error=")
    assert "unbound" in location


# ---------------------------------------------------------------------------
# The Markdown route
# ---------------------------------------------------------------------------


async def test_playbook_route_serves_markdown_named_for_the_workflow(
    make_workflow: Any,
) -> None:
    slug = f"pytest-playbook-{uuid.uuid4().hex[:8]}"
    workflow_id = make_workflow(STEPS, slug=slug)

    response = await build_router().dispatch(
        Request(
            method="GET",
            path=f"/api/workflows/{workflow_id}/playbook.md",
            principal=SME,
        )
    )
    assert response.status == HTTPStatus.OK
    assert response.content_type == "text/markdown; charset=utf-8"
    assert response.headers["content-disposition"] == f'attachment; filename="{slug}-v1.md"'
    assert isinstance(response.body, str)
    assert response.body.startswith("---\n")
    assert "## Steps" in response.body
    assert "Approve this discount?" in response.body


async def test_playbook_route_404s_for_a_workflow_that_is_not_there() -> None:
    response = await build_router().dispatch(
        Request(method="GET", path="/api/workflows/999999999/playbook.md", principal=SME)
    )
    assert response.status == HTTPStatus.NOT_FOUND
    assert response.content_type == "application/json"
    assert response.body == {"error": "workflow 999999999 not found"}


async def test_the_playbook_route_does_not_shadow_the_json_workflow_route(
    make_workflow: Any,
) -> None:
    """`/api/workflows/{id}` must not swallow `/api/workflows/{id}/playbook.md`."""
    workflow_id = make_workflow(STEPS)
    router = build_router()

    as_json = await router.dispatch(
        Request(method="GET", path=f"/api/workflows/{workflow_id}", principal=SME)
    )
    assert as_json.content_type == "application/json"
    assert isinstance(as_json.body, dict)
    assert as_json.body["workflow_id"] == workflow_id

    as_markdown = await router.dispatch(
        Request(method="GET", path=f"/api/workflows/{workflow_id}/playbook.md", principal=SME)
    )
    assert as_markdown.content_type.startswith("text/markdown")


# ---------------------------------------------------------------------------
# One document, three surfaces
# ---------------------------------------------------------------------------


async def test_the_gate_and_the_mcp_tool_render_identical_bytes(make_workflow: Any) -> None:
    """The whole reason the renderer sits in fde-mcp rather than in fde-gate.

    The two packages hold their own copies of the SELECTs that feed it --
    they run as different database roles -- so this is what catches a column
    added to one and forgotten in the other.
    """
    workflow_id = make_workflow(STEPS)

    from_gate = await workflows.get_playbook(workflow_id)
    from_mcp = await mcp_workflow.wf_export_playbook(workflow_id)

    assert "error" not in from_mcp, from_mcp
    assert from_gate["markdown"] == from_mcp["markdown"]


async def test_the_page_and_the_document_cite_bindings_in_one_order(make_workflow: Any) -> None:
    """The page renders bindings as a table, not through the renderer.

    They are sorted once in the service so both see the same rows: a step
    whose citations read `implements` then `depends_on` in the download and
    the other way round on the page looks like two different sets of facts
    about the same step.
    """
    workflow_id = make_workflow(
        [
            {
                "step_key": "grounded",
                "kind": "tool",
                "title": "A step with two citations",
                "bindings": (
                    (BOUND_NODE_KEYS[0], "depends_on"),
                    (BOUND_NODE_KEYS[1], "implements"),
                ),
            }
        ]
    )
    playbook = await workflows.get_playbook(workflow_id)
    assert [b["relation"] for b in playbook["steps"][0]["bindings"]] == [
        "implements",
        "depends_on",
    ]

    page = await ui.workflow_page(_get(f"/ui/workflows/{workflow_id}", workflow_id))
    assert isinstance(page.body, str)
    assert page.body.index("implements") < page.body.index("depends_on")
    assert playbook["markdown"].index("implements") < playbook["markdown"].index("depends_on")


async def test_the_console_page_and_the_export_read_the_same_transaction(
    make_workflow: Any,
) -> None:
    """`get_playbook` is one query set; the page and the file cannot disagree."""
    workflow_id = make_workflow(STEPS)
    playbook = await workflows.get_playbook(workflow_id)

    assert [step["step_key"] for step in playbook["steps"]] == ["pull_quote", "deal_desk"]
    for step in playbook["steps"]:
        assert f"### {step['ordinal']}. {step['title']}" in playbook["markdown"]
    assert playbook["workflow"]["status"] == "published"
    assert "Status **published**" in playbook["markdown"]
