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


def test_exactly_one_markup_call_site_in_the_console() -> None:
    """The other half of the same claim, and the half that can actually rot.

    "No `|safe` in any template" only holds the line if the Python side does
    not quietly grow a second escape hatch. One sanctioned call site exists —
    `dashboard_page`, wrapping HTML that `fde_mcp.dashboard` has already
    escaped — and a second would be a new trust boundary that nobody argued
    for. Counting is the whole test: it fails on the second one appearing,
    which is the moment to write the argument or not add it.
    """
    source = Path(ui.__file__).read_text()
    calls = re.findall(r"\bMarkup\(", source)
    assert len(calls) == 1, (
        f"expected exactly one Markup() call site in ui.py, found {len(calls)}. "
        "If the new one is deliberate, say why beside it and update this count."
    )
    # And it is the one we think it is.
    assert 'dashboard_html=Markup(built["html"])' in source


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


def test_the_window_policy_is_one_function() -> None:
    """Offered values survive; everything else lands on the default."""
    for good in dashboard.WINDOW_CHOICES:
        assert dashboard.normalize_window(str(good)) == good
    for bad in ("45", "nonsense", "-1", "999999", "0", "", None):
        assert dashboard.normalize_window(bad) == dashboard.DEFAULT_WINDOW_DAYS


async def test_the_page_and_its_download_read_one_url_the_same_way() -> None:
    """The bug this replaced: `?window_days=45` showed 30 days on the page and
    45 in the file it links to, so "the same numbers as a file" was not true
    for any hand-edited or shared URL."""
    for raw in ("45", "7", "nonsense", "999999"):
        page = await ui.dashboard_page(_get("/ui/dashboard", {"window_days": raw}))
        download = await build_router().dispatch(
            Request(
                method="GET",
                path="/api/dashboard.md",
                query={"window_days": raw},
                principal=SME,
            )
        )
        assert isinstance(page.body, str)
        assert isinstance(download.body, str)
        expected = dashboard.normalize_window(raw)
        assert f"last {expected} days" in page.body, raw
        assert f"window: last {expected} days" in download.body, raw
        assert f"window_days: {expected}" in download.body, raw


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


_MD_WAITING = re.compile(r"- \*\*(\d+)\*\* proposals waiting on a human")
_MD_OVERDUE = re.compile(r"- \*\*(\d+)\*\* gates overdue, (\d+) pending, (\d+) cleared")
_HTML_TILE = re.compile(r'<span class="k">(.*?)</span><span class="v">(.*?)</span>')


async def test_one_build_renders_both_documents_from_one_assembly(
    make_proposal: Any, seed: dict[str, Any]
) -> None:
    """The console and the export are read by different people who then talk
    to each other, so they must not be able to disagree.

    This asserts the console-level half of that: that ONE `build()` produces
    both documents from ONE assembly at ONE instant. It reads the figures out
    of each document by their labels and compares them to the assembled dict,
    rather than asking whether a small integer appears somewhere in the text
    -- in a document full of counts, "is there a 2 in it" is true almost
    however broken the renderer is.

    The renderer-level md/html parity claim lives in
    `test_dashboard_render.py::test_the_two_renderers_report_the_same_numbers`,
    over a hand-built fixture whose figures are all distinct. This one runs
    against a live database, where they are not: a submitted proposal makes
    `cleared` and `overdue` both zero, so swapping those two here would prove
    nothing. It therefore asserts only the pair this fixture can actually
    tell apart, and asserts that it can -- the `total > open` precondition
    below is what stops this test going quietly vacuous if the factory
    changes.
    """
    make_proposal(title="pytest dashboard parity")
    # A draft is deliberately NOT waiting on anyone (it was never submitted),
    # so it lands in `total` and not in `open`, which is what makes the two
    # figures distinguishable and the comparison below worth making.
    make_proposal(title="pytest dashboard parity draft", submit=False)

    built = await dashboard.build(engagement_id=seed["engagement_id"], window_days=30)
    data, markdown, html = built["data"], built["markdown"], built["html"]

    assert data["queue"]["total"] > data["queue"]["open"], (
        "this fixture must distinguish 'waiting' from 'on record', or the "
        "assertions below pass however the renderer confuses them"
    )

    waiting = _MD_WAITING.search(markdown)
    gates = _MD_OVERDUE.search(markdown)
    assert waiting is not None, "the Markdown headline lost its waiting figure"
    assert gates is not None, "the Markdown headline lost its gate figures"

    tiles = dict(_HTML_TILE.findall(html))
    assert set(tiles) >= {"Waiting on a human", "Gates overdue"}, tiles

    # Markdown against the assembly.
    assert int(waiting.group(1)) == data["queue"]["open"]
    assert int(gates.group(1)) == data["gates"]["overdue"]
    assert int(gates.group(2)) == data["gates"]["pending"]
    assert int(gates.group(3)) == data["gates"]["cleared"]

    # HTML against the same assembly -- so the two agree with each other by
    # both agreeing with it, at one instant rather than two.
    assert int(tiles["Waiting on a human"]) == data["queue"]["open"]
    assert int(tiles["Gates overdue"]) == data["gates"]["overdue"]


async def test_the_page_serves_the_html_the_service_rendered(
    make_proposal: Any, seed: dict[str, Any]
) -> None:
    """The page must EMBED the service's document, not re-derive one.

    Checked structurally rather than by byte-comparing two builds: two builds
    are two instants and their `generated_at` differ by construction.
    """
    make_proposal(title="pytest dashboard embed")
    page = await ui.dashboard_page(_get("/ui/dashboard", {"engagement_id": seed["engagement_id"]}))
    assert isinstance(page.body, str)
    assert page.body.count('<section class="fde-dash">') == 1
    assert page.body.count("</section>") == 1
    # The scoped stylesheet travels with the fragment, exactly once.
    assert page.body.count(".fde-dash{color-scheme:light;") == 1


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
