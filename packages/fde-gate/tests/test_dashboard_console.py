"""The dashboard's two console surfaces, and the privilege split behind them.

`/ui/dashboard` and `GET /api/dashboard.md` render one document through
`fde_mcp.dashboard`. What is worth testing here rather than in the renderer's
own unit tests is the thing only a live database can show: that the page
assembles from TWO roles, and that both panels the gate role owns actually
arrive. `fde_prodops` cannot read `hitl.reviewer` or `wf.agent_launch`, so a
regression that dropped the second transaction would still render a complete-
looking page -- with the reviewer table quietly replaced by a grant notice.
"""

from __future__ import annotations

import re
from http import HTTPStatus
from pathlib import Path
from typing import Any

import psycopg
import pytest
from gate_seed import SME

from fde_gate import ui
from fde_gate.config import get_gate_settings
from fde_gate.handler import build_router
from fde_gate.http import Request
from fde_gate.service import dashboard
from fde_mcp import db

pytestmark = pytest.mark.requires_db


def _get(path: str, query: dict[str, str] | None = None, principal: str = SME) -> Request:
    return Request(method="GET", path=path, query=query or {}, principal=principal)


# ---------------------------------------------------------------------------
# The page
# ---------------------------------------------------------------------------


async def test_the_dashboard_page_renders_every_panel(make_proposal: Any) -> None:
    make_proposal(title="pytest dashboard proposal")
    page = await ui.dashboard_page(_get("/ui/dashboard"))

    assert page.status == HTTPStatus.OK
    assert page.content_type.startswith("text/html")
    assert isinstance(page.body, str)
    text = " ".join(page.body.split())

    for heading in (
        "Review queue",
        "Gates",
        "Decisions by reviewer",
        "Merges",
        "Drift",
        "Workflow runs",
        "Agent launches",
    ):
        assert heading in text, f"the {heading} panel is missing from the page"


async def test_the_page_reads_the_panels_prodops_cannot(make_proposal: Any) -> None:
    """The whole reason this page opens two transactions.

    `fde_prodops` holds no SELECT on `hitl.reviewer` or `wf.agent_launch`. If
    the gate-role read were dropped, both panels would render their grant
    notice instead of their data -- which is a page that still looks fine.
    """
    make_proposal(title="pytest dashboard two-role")
    page = await ui.dashboard_page(_get("/ui/dashboard"))
    assert isinstance(page.body, str)

    assert "cannot see hitl.reviewer" not in page.body
    assert "cannot see wf.agent_launch" not in page.body


def test_no_template_in_this_console_uses_safe() -> None:
    """The dashboard's pre-rendered HTML is marked safe in Python, once.

    `ui.dashboard_page` wraps it in `markupsafe.Markup` at the construction
    site, where the trust argument sits beside the value and a reviewer can
    audit it. `|safe` in a template would be the same bypass expressed as
    syntax the next page can copy without the argument -- which is how a
    console that escapes everything acquires a page that does not. This test
    is the difference between that being a decision and a convention.
    """
    templates = Path(ui.__file__).parent / "templates"
    # `{# ... #}` blocks are stripped first: dashboard.html.j2's comment says
    # the words "no `|safe` appears in any template", and a test that counted
    # its own explanation as a violation would be unfixable without deleting
    # the explanation.
    comments = re.compile(r"\{#.*?#\}", re.S)
    offenders = [
        path.name
        for path in sorted(templates.glob("*.html.j2"))
        if re.search(r"\|\s*safe\b", comments.sub("", path.read_text()))
    ]
    assert offenders == [], f"templates using |safe: {offenders}"


async def test_the_page_carries_the_live_state_disclosure() -> None:
    page = await ui.dashboard_page(_get("/ui/dashboard"))
    assert isinstance(page.body, str)
    assert "not a pinned artefact" in page.body


async def test_the_page_offers_the_markdown_download_with_the_same_scope(
    seed: dict[str, Any],
) -> None:
    """The link has to carry the filter, or the download is a different view
    from the one on screen."""
    engagement_id = seed["engagement_id"]
    page = await ui.dashboard_page(
        _get("/ui/dashboard", {"engagement_id": engagement_id, "window_days": "7"})
    )
    assert isinstance(page.body, str)
    assert "/api/dashboard.md?" in page.body
    assert f"engagement_id={engagement_id}" in page.body
    assert "window_days=7" in page.body


async def test_an_unoffered_window_falls_back_rather_than_refusing() -> None:
    """A stale bookmark should show a dashboard, not an error page."""
    for bad in ("45", "nonsense", "-1", "999999"):
        page = await ui.dashboard_page(_get("/ui/dashboard", {"window_days": bad}))
        assert page.status == HTTPStatus.OK
        assert isinstance(page.body, str)
        assert "last 30 days" in page.body, f"{bad!r} should fall back to the default"


async def test_the_engagement_filter_scopes_the_page(seed: dict[str, Any]) -> None:
    engagement_id = seed["engagement_id"]
    scoped = await ui.dashboard_page(_get("/ui/dashboard", {"engagement_id": engagement_id}))
    rollup = await ui.dashboard_page(_get("/ui/dashboard"))

    assert isinstance(scoped.body, str)
    assert isinstance(rollup.body, str)
    assert engagement_id in scoped.body
    assert "all engagements" in rollup.body


async def test_the_page_does_not_repeat_its_own_title() -> None:
    """The template supplies the <h1>; the embedded section must not add a
    second heading saying the same thing three lines below it."""
    page = await ui.dashboard_page(_get("/ui/dashboard"))
    assert isinstance(page.body, str)
    assert page.body.count("Decision dashboard</h") == 1


async def test_the_nav_links_to_the_dashboard() -> None:
    """A page nobody can find from the chrome is a page nobody opens."""
    page = await ui.dashboard_page(_get("/ui/dashboard"))
    assert isinstance(page.body, str)
    assert '<a href="/ui/dashboard">Dashboard</a>' in page.body


# ---------------------------------------------------------------------------
# The .md route
# ---------------------------------------------------------------------------


async def test_the_md_route_serves_markdown_dated_for_the_day(
    make_proposal: Any,
) -> None:
    make_proposal(title="pytest dashboard md")
    response = await build_router().dispatch(
        Request(method="GET", path="/api/dashboard.md", principal=SME)
    )

    assert response.status == HTTPStatus.OK
    assert response.content_type == "text/markdown; charset=utf-8"
    assert isinstance(response.body, str)
    assert response.body.startswith("---\n")
    assert "# Decision dashboard" in response.body
    assert "pinned: false" in response.body

    disposition = response.headers["content-disposition"]
    # Inline, not an attachment: a playbook is a document you keep, this is a
    # reading of a moment. The date is what stops two of them colliding.
    assert disposition.startswith('inline; filename="fde-dashboard-')
    assert disposition.endswith('.md"')


async def test_the_md_route_honours_the_engagement_filter(seed: dict[str, Any]) -> None:
    engagement_id = seed["engagement_id"]
    response = await build_router().dispatch(
        Request(
            method="GET",
            path="/api/dashboard.md",
            query={"engagement_id": engagement_id},
            principal=SME,
        )
    )
    assert response.status == HTTPStatus.OK
    assert isinstance(response.body, str)
    assert engagement_id in response.body


async def test_the_md_route_is_reachable_by_any_authenticated_principal() -> None:
    """Matching /ui/runs and /ui/drift: aggregate counts over work the console
    already shows every principal in detail. Gating the summary would hide it
    from people who can read every row it summarises."""
    response = await build_router().dispatch(
        Request(method="GET", path="/api/dashboard.md", principal="stranger@example.com")
    )
    assert response.status == HTTPStatus.OK


async def test_the_md_route_does_not_shadow_the_review_queue_route() -> None:
    """Both hang off /api/; a greedy pattern on one would swallow the other."""
    router = build_router()
    queue = await router.dispatch(Request(method="GET", path="/api/review-queue", principal=SME))
    assert queue.content_type == "application/json"

    markdown = await router.dispatch(Request(method="GET", path="/api/dashboard.md", principal=SME))
    assert markdown.content_type == "text/markdown; charset=utf-8"


# ---------------------------------------------------------------------------
# The two surfaces are one document
# ---------------------------------------------------------------------------


async def test_the_page_and_the_download_report_the_same_numbers(
    make_proposal: Any, seed: dict[str, Any]
) -> None:
    """The console and the export are read by different people who then talk
    to each other. One assembly serves both, and this is what holds it there.
    """
    make_proposal(title="pytest dashboard parity")
    engagement_id = seed["engagement_id"]

    built = await dashboard.build(engagement_id=engagement_id, window_days=30)
    page = await ui.dashboard_page(_get("/ui/dashboard", {"engagement_id": engagement_id}))
    assert isinstance(page.body, str)

    data = built["data"]
    # The headline figures, in the rendered HTML the page actually shows.
    assert f'<span class="v">{data["queue"]["open"]}</span>' in page.body
    assert f'<span class="v">{data["gates"]["overdue"]}</span>' in page.body

    for count in (data["gates"]["cleared"], data["gates"]["pending"]):
        assert str(count) in built["markdown"]


async def test_the_service_reports_the_engagements_it_can_offer(
    seed: dict[str, Any],
) -> None:
    built = await dashboard.build()
    assert seed["engagement_id"] in built["engagements"]


# ---------------------------------------------------------------------------
# The grant boundary itself
# ---------------------------------------------------------------------------


async def test_prodops_really_cannot_read_the_two_gate_role_tables(
    seed: dict[str, Any],
) -> None:
    """The premise of this whole module, asserted rather than assumed.

    If a future migration widened `fde_prodops`, the two-transaction split
    above would become dead weight nobody could tell was unnecessary -- and,
    more to the point, db/018's denial would have been undone without anyone
    noticing here.
    """
    for table in ("hitl.reviewer", "wf.agent_launch"):
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            async with (
                db.tool_transaction(role=get_gate_settings().gate.prodops_role) as conn,
                conn.cursor() as cur,
            ):
                await cur.execute(f"SELECT 1 FROM {table} LIMIT 1")  # noqa: S608
