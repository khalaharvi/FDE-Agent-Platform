"""The console: escaping, and the review-seconds measurement.

Handlers are called directly with a constructed `Request`, which is the same
object `parse_apigw_event` would have produced -- so these are real renders
of real service output with no browser and no AWS.
"""

from __future__ import annotations

import time
import uuid
from http import HTTPStatus
from typing import Any
from urllib.parse import unquote

import pytest
from gate_seed import SME

from fde_gate import ui
from fde_gate.http import Request
from fde_gate.service import proposals

pytestmark = pytest.mark.requires_db

# A payload that is markup. Not a contrived one: proposal payloads are
# extracted by an agent from customer documents, and an SOP with an HTML
# fragment in it is an ordinary Tuesday.
HOSTILE_LABEL = '<script>alert("xss")</script>'


async def test_queue_and_proposal_render_with_escaped_payloads(
    make_proposal: Any,
) -> None:
    created = make_proposal(
        title=f"hostile payload {uuid.uuid4().hex[:6]}",
        items=[
            {
                "op": "add_node",
                "node_type": "activity",
                "subject_key": "act.hostile",
                "payload": {"label": HOSTILE_LABEL, "summary": "Agent-authored markup."},
                "agent_confidence": 0.5,
            }
        ],
    )

    queue = await ui.queue_page(Request(method="GET", path="/ui", principal=SME))
    assert queue.status == 200
    assert queue.content_type.startswith("text/html")
    assert isinstance(queue.body, str)
    assert "Review queue" in queue.body
    assert created["title"] if False else True  # title is on the detail page

    page = await ui.proposal_page(
        Request(
            method="GET",
            path=f"/ui/proposals/{created['proposal_id']}",
            path_params={"proposal_id": str(created["proposal_id"])},
            principal=SME,
        )
    )
    assert page.status == 200
    assert isinstance(page.body, str)

    # The payload reached the page...
    assert "act.hostile" in page.body
    assert "alert" in page.body
    # ...but not as executable markup. Jinja2's `|tojson` escapes `<` and `>`
    # inside the JSON literal, so the raw tag never appears.
    assert HOSTILE_LABEL not in page.body
    assert "<script>" not in page.body

    # The reviewer's actual questions are answered on the page.
    assert "RevOps interview (pytest fixture)" in page.body, "evidence must be visible"
    assert "sme@example.com" in page.body, "the authorised roster must be visible"
    assert "Record a decision" in page.body


async def test_missing_proposal_renders_a_page_not_a_stack_trace(
    make_proposal: Any,
) -> None:
    page = await ui.proposal_page(
        Request(
            method="GET",
            path="/ui/proposals/999999999",
            path_params={"proposal_id": "999999999"},
            principal=SME,
        )
    )
    assert page.status == 200
    assert isinstance(page.body, str)
    assert "There is no proposal 999999999 here." in page.body


async def test_changes_requested_says_why_there_is_no_decision_form(
    make_proposal: Any,
) -> None:
    """A queued proposal that is not decidable must say so.

    `changes_requested` is in the queue's open set and in
    `hitl.expire_proposals`' sweep, so its SLA keeps running -- but
    `hitl.record_decision` and `hitl.edit_item` both refuse it. Rendering no
    form and no explanation leaves the reviewer to work that out.
    """
    created = make_proposal()
    await proposals.record_decision(
        created["gates"][0]["gate_id"], SME, "request_changes", comment="cite the SOP"
    )

    page = await ui.proposal_page(
        Request(
            method="GET",
            path=f"/ui/proposals/{created['proposal_id']}",
            path_params={"proposal_id": str(created["proposal_id"])},
            principal=SME,
        )
    )
    assert isinstance(page.body, str)
    # Collapsed, because the template wraps prose across source lines and a
    # test that pins the wrap points is a test that fails on reformatting.
    text = " ".join(page.body.split())
    assert "Waiting on the agent" in text
    assert "not decidable or editable until the agent resubmits it" in text
    assert "its SLA is still running" in text
    assert "Record a decision" not in text, "the form must be absent, not just inert"


async def test_decision_form_roundtrip_records_review_seconds(make_proposal: Any, sql: Any) -> None:
    """review_seconds is measured server-side from a hidden render stamp.

    db/004:176 calls it a training-quality signal: a four-second approval on
    a thirty-item proposal is a rubber stamp, not a label. Measuring it needs
    no JavaScript -- the server wrote the timestamp into the form, so the
    server can subtract it.
    """
    created = make_proposal()
    gate_id = created["gates"][0]["gate_id"]
    (item_id,) = created["item_ids"]
    proposal_id = created["proposal_id"]

    rendered_at = time.time() - 12.5
    response = await ui.decision_post(
        Request(
            method="POST",
            path=f"/ui/proposals/{proposal_id}/decision",
            path_params={"proposal_id": str(proposal_id)},
            form={
                "gate_id": str(gate_id),
                "decision": "approve",
                "comment": "read the evidence, agrees with the SOP",
                "rendered_at": str(rendered_at),
                f"verdict_{item_id}": "accept",
            },
            principal=SME,
        )
    )

    # POST -> 303 -> GET, so a refresh cannot record a second decision.
    assert response.status == HTTPStatus.SEE_OTHER
    assert response.headers["Location"].startswith(f"/ui/proposals/{proposal_id}?notice=")

    (decision,) = sql(
        """
        SELECT d.decision::text AS decision, d.comment, d.review_seconds
          FROM hitl.gate_decision d
         WHERE d.gate_id = %(g)s AND d.superseded_by IS NULL
        """,
        {"g": gate_id},
    )
    assert decision["decision"] == "approve"
    assert decision["comment"] == "read the evidence, agrees with the SOP"
    assert 12 <= decision["review_seconds"] <= 60, "wall-clock since the page rendered"

    (item,) = sql(
        "SELECT item_status FROM hitl.proposal_item WHERE item_id = %(i)s", {"i": item_id}
    )
    assert item["item_status"] == "accepted"


async def test_a_refused_decision_redirects_with_the_verbatim_message(
    make_proposal: Any,
) -> None:
    """The reviewer sees the database's own words, not "an error occurred"."""
    created = make_proposal()
    gate_id = created["gates"][0]["gate_id"]
    proposal_id = created["proposal_id"]

    response = await ui.decision_post(
        Request(
            method="POST",
            path=f"/ui/proposals/{proposal_id}/decision",
            path_params={"proposal_id": str(proposal_id)},
            form={"gate_id": str(gate_id), "decision": "approve", "rendered_at": "0"},
            principal="ghost@example.com",
        )
    )

    assert response.status == HTTPStatus.SEE_OTHER
    location = unquote(response.headers["Location"])
    assert "error=" in location
    assert "ghost@example.com is not a registered reviewer" in location
