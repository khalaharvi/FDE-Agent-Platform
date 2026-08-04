"""The dashboard renderers: one data dict, two documents that must agree.

No database here. `assemble_dashboard` takes rows and returns a dict; the two
renderers take that dict and return strings. All three are pure, so every
test below fixes `now` and asserts on bytes.

The test that matters most is
`test_the_two_renderers_report_the_same_numbers`: the Markdown export and the
console's HTML are read by different people who then talk to each other, and
a count that differs between them is worse than either being absent.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from typing import Any

from fde_mcp.dashboard import (
    assemble_dashboard,
    render_dashboard_html,
    render_dashboard_markdown,
)

# Frozen. Every "overdue", every window boundary and every age below is
# relative to this instant and to nothing the clock is doing while the suite
# runs.
NOW = datetime(2026, 8, 3, 12, 0, 0, tzinfo=UTC)
ENGAGEMENT = "11111111-1111-1111-1111-111111111111"

# A reviewer principal that is markup. The real roster is all @example.com,
# but `hitl.reviewer.principal` is a text column an administrator types into,
# and a renderer that is safe only because of what happens to be in the table
# is not safe. See `_esc` in the module under test.
HOSTILE_PRINCIPAL = '<script>alert("xss")</script>@example.com'


def _rows() -> dict[str, Any]:
    """A populated dashboard: every panel non-empty, every optional present."""
    return {
        "queue": [
            {
                "status": "submitted",
                "n": 4,
                "oldest_submitted_at": NOW - timedelta(days=2),
            },
            {
                "status": "in_review",
                "n": 2,
                "oldest_submitted_at": NOW - timedelta(hours=6),
            },
            {"status": "merged", "n": 7, "oldest_submitted_at": NOW - timedelta(days=9)},
        ],
        "gates": {
            "cleared": 11,
            "pending": 3,
            "overdue": 2,
            "next_due_at": NOW + timedelta(hours=5),
            "median_seconds_to_clear": 9000.0,  # 2.5h
        },
        "gate_kinds": [
            {"gate_kind": "factual", "cleared": 6, "open": 2, "overdue": 1},
            {"gate_kind": "control", "cleared": 5, "open": 3, "overdue": 1},
        ],
        "reviewers": [
            {
                "principal": "sme@example.com",
                "approvals": 8,
                "rejections": 1,
                "changes_requested": 2,
                "abstentions": 0,
                "decisions": 11,
                "last_decided_at": NOW - timedelta(hours=3),
            }
        ],
        "merges": {
            "merges_in_window": 5,
            "sealed_total": 12,
            "latest_sealed_at": NOW - timedelta(hours=1),
            "latest_digest": "abc123def456",
        },
        "drift": [
            {"state": "open", "n": 3},
            {"state": "triaged", "n": 1},
        ],
        "runs": [
            {"status": "succeeded", "n": 6},
            {"status": "failed", "n": 1},
        ],
        "launches": [
            {"status": "succeeded", "n": 2},
            {"status": "running", "n": 1},
        ],
    }


def _assemble(**overrides: Any) -> dict[str, Any]:
    engagement_id = overrides.pop("engagement_id", ENGAGEMENT)
    return assemble_dashboard(
        engagement_id=engagement_id,
        window_days=30,
        now=NOW,
        **{**_rows(), **overrides},
    )


def _empty() -> dict[str, Any]:
    """What a freshly rebuilt database renders: nothing anywhere."""
    return assemble_dashboard(
        engagement_id=ENGAGEMENT,
        window_days=30,
        now=NOW,
        queue=[],
        gates={
            "cleared": 0,
            "pending": 0,
            "overdue": 0,
            "next_due_at": None,
            "median_seconds_to_clear": None,
        },
        gate_kinds=[],
        reviewers=[],
        merges={
            "merges_in_window": 0,
            "sealed_total": 0,
            "latest_sealed_at": None,
            "latest_digest": None,
        },
        drift=[],
        runs=[],
        launches=[],
    )


# ---------------------------------------------------------------------------
# Assembly
# ---------------------------------------------------------------------------


def test_only_undecided_proposals_count_as_waiting() -> None:
    """`merged` is finished work. Counting it as a backlog would make a
    healthy engagement look like a stalled one."""
    data = _assemble()
    assert data["queue"]["open"] == 6, "submitted 4 + in_review 2, and not the 7 merged"
    assert data["queue"]["total"] == 13


def test_the_oldest_waiting_ignores_finished_work() -> None:
    """The 9-day-old row is `merged`; nobody is waiting on it."""
    data = _assemble()
    assert data["queue"]["oldest_waiting_at"] == (NOW - timedelta(days=2)).isoformat()
    assert data["queue"]["oldest_waiting_age"] == "2.0d"


def test_an_approved_proposal_is_still_waiting_on_a_human() -> None:
    """`approved` means every gate cleared and nobody has merged it yet.

    `hitl.merge_proposal` refuses any status but `approved` (db/005:84) and
    nothing calls it automatically, so the proposal is parked in front of the
    one human write that touches the graph. Excluding it reported an
    all-approved queue as empty -- the most misleading possible reading, on
    the panel whose entire job is "how much is waiting".

    Asserts all three places the status reaches: the open count, the
    oldest-waiting timestamp, and the age string the KPI tile renders.
    """
    data = _assemble(
        queue=[
            {
                "status": "approved",
                "n": 2,
                "oldest_submitted_at": NOW - timedelta(days=4),
            },
            {"status": "merged", "n": 5, "oldest_submitted_at": NOW - timedelta(days=1)},
        ]
    )

    assert data["queue"]["open"] == 2, "an approved, unmerged proposal is still waiting"
    assert data["queue"]["oldest_waiting_at"] == (NOW - timedelta(days=4)).isoformat()
    assert data["queue"]["oldest_waiting_age"] == "4.0d"

    # And it reaches the reader, in both documents.
    markdown = render_dashboard_markdown(data)
    html_text = _HTML_TAG.sub(" ", render_dashboard_html(data))
    assert "**2** proposals waiting on a human" in markdown
    assert "4.0d" in markdown
    assert "4.0d" in html_text


def test_a_queue_of_only_approved_work_does_not_read_as_empty() -> None:
    """The regression in its starkest form: before `approved` was counted,
    this rendered "0 proposals waiting" and "Oldest still waiting: never"."""
    data = _assemble(
        queue=[
            {
                "status": "approved",
                "n": 3,
                "oldest_submitted_at": NOW - timedelta(hours=30),
            }
        ]
    )
    assert data["queue"]["open"] == 3
    assert data["queue"]["oldest_waiting_age"] != "never"
    assert "**0** proposals waiting on a human" not in render_dashboard_markdown(data)


def test_statuses_render_in_lifecycle_order_not_alphabetical() -> None:
    data = _assemble()
    assert [row["status"] for row in data["queue"]["by_status"]] == [
        "submitted",
        "in_review",
        "merged",
    ]


def test_an_unknown_status_still_appears_rather_than_vanishing() -> None:
    """A status added to the schema must not silently drop off the page."""
    data = _assemble(
        queue=[
            {"status": "merged", "n": 1, "oldest_submitted_at": None},
            {"status": "quantum_superposed", "n": 2, "oldest_submitted_at": None},
        ]
    )
    statuses = [row["status"] for row in data["queue"]["by_status"]]
    assert statuses == ["merged", "quantum_superposed"], "unknown sorts after known"
    assert data["queue"]["total"] == 3


def test_open_drift_excludes_closed_states() -> None:
    data = _assemble(
        drift=[
            {"state": "open", "n": 2},
            {"state": "triaged", "n": 1},
            {"state": "resolved", "n": 5},
            {"state": "dismissed", "n": 4},
        ]
    )
    assert data["drift"]["open"] == 3, "open + triaged; not resolved or dismissed"
    assert data["drift"]["total"] == 12


def test_an_uncomputable_median_is_not_reported_as_zero() -> None:
    """NULL from `percentile_cont` over an empty set means nothing has
    cleared, which is the opposite of everything clearing instantly."""
    data = _empty()
    assert data["gates"]["median_seconds_to_clear"] is None
    assert "not computable yet" in render_dashboard_markdown(data)
    assert "0s" not in render_dashboard_markdown(data)


# ---------------------------------------------------------------------------
# The privilege partition: None is not []
# ---------------------------------------------------------------------------


def test_unreadable_launches_are_declared_not_omitted() -> None:
    """`launches=None` is "this role may not look", and the page must say so.

    An operator who read a dashboard with no launches section would conclude
    no agents ran. The one thing this panel must never do is imply that.
    """
    data = _assemble(launches=None)
    assert data["launches"] is None

    markdown = render_dashboard_markdown(data)
    html = render_dashboard_html(data)
    for rendered in (markdown, html):
        assert "wf.agent_launch" in rendered, "name the table so the reason is checkable"
        assert "db/018" in rendered, "cite the grant that withholds it"
        assert "not a statement that no agents ran" in rendered


def test_no_launches_reads_differently_from_unreadable_launches() -> None:
    """The empty case and the forbidden case must not render the same words."""
    forbidden = render_dashboard_markdown(_assemble(launches=None))
    empty = render_dashboard_markdown(_assemble(launches=[]))

    assert "No agent launches requested" in empty
    assert "No agent launches requested" not in forbidden
    assert "wf.agent_launch" not in empty
    assert forbidden != empty


def test_unreadable_reviewers_are_declared_not_omitted() -> None:
    """The same rule for the panel `fde_prodops` cannot read."""
    data = _assemble(reviewers=None)
    assert data["reviewers"] is None
    for rendered in (render_dashboard_markdown(data), render_dashboard_html(data)):
        assert "hitl.reviewer" in rendered
        assert "not a statement that nobody has decided anything" in rendered


def test_the_shared_disclosures_carry_no_markdown_syntax_into_the_html() -> None:
    """The three shared constants are plain prose; each renderer adds its own
    emphasis. A stray `**` in the HTML reads as a typo and stops being read."""
    html = render_dashboard_html(_assemble(launches=None, reviewers=None))
    blocked = re.findall(r'<p class="dash-blocked">(.*?)</p>', html, re.S)
    assert len(blocked) == 2
    for note in blocked:
        assert "**" not in note
        assert "`" not in note


# ---------------------------------------------------------------------------
# The anti-stability disclosure
# ---------------------------------------------------------------------------


def test_both_documents_say_they_are_live_state() -> None:
    """The surface next door (`wf_export_playbook`) guarantees byte-stability.
    A reader who assumes the same here will act on a stale count."""
    data = _assemble()
    for rendered in (render_dashboard_markdown(data), render_dashboard_html(data)):
        assert "not a pinned artefact" in rendered
        assert "cites no individual proposal as evidence" in rendered


def test_the_front_matter_marks_the_document_unpinned() -> None:
    """A vault query has to be able to tell these from the playbooks."""
    assert "pinned: false" in render_dashboard_markdown(_assemble())


# ---------------------------------------------------------------------------
# The two renderers must agree
# ---------------------------------------------------------------------------

_HTML_TAG = re.compile(r"<[^>]+>")


def _numbers(text: str) -> list[str]:
    return re.findall(r"\b\d+\b", text)


_MD_WAITING = re.compile(r"- \*\*(\d+)\*\* proposals waiting on a human \(of (\d+) on record\)")
_MD_GATES = re.compile(r"- \*\*(\d+)\*\* gates overdue, (\d+) pending, (\d+) cleared")
_MD_MERGES = re.compile(r"- \*\*(\d+)\*\* merges in the window \((\d+) sealed commits in total\)")
_MD_DRIFT = re.compile(r"- \*\*(\d+)\*\* drift signals still open")
_HTML_TILE = re.compile(r'<span class="k">(.*?)</span><span class="v">(.*?)</span>')


def test_the_two_renderers_report_the_same_numbers() -> None:
    """Every headline figure, read out of both documents BY ITS LABEL.

    Not a byte comparison -- they are different formats -- and deliberately
    not "does this integer appear anywhere in the text" either. In a document
    that is nothing but counts, asking whether a `2` occurs somewhere is
    satisfied by almost any bug: two figures could swap and both assertions
    would still pass. So each number is extracted from the position that
    claims to hold it, and the fixture in `_rows` keeps every headline figure
    distinct (6, 13, 2, 3, 11, 5, 12, 3) so a swap has somewhere to show up.
    """
    data = _assemble()
    markdown = render_dashboard_markdown(data)
    html = render_dashboard_html(data)

    waiting = _MD_WAITING.search(markdown)
    gates = _MD_GATES.search(markdown)
    merges = _MD_MERGES.search(markdown)
    drift = _MD_DRIFT.search(markdown)
    assert waiting is not None, "the Markdown headline lost its waiting figure"
    assert gates is not None, "the Markdown headline lost its gate figures"
    assert merges is not None, "the Markdown headline lost its merge figures"
    assert drift is not None, "the Markdown headline lost its drift figure"

    assert int(waiting.group(1)) == data["queue"]["open"]
    assert int(waiting.group(2)) == data["queue"]["total"]
    assert int(gates.group(1)) == data["gates"]["overdue"]
    assert int(gates.group(2)) == data["gates"]["pending"]
    assert int(gates.group(3)) == data["gates"]["cleared"]
    assert int(merges.group(1)) == data["merges"]["in_window"]
    assert int(merges.group(2)) == data["merges"]["sealed_total"]
    assert int(drift.group(1)) == data["drift"]["open"]

    # The HTML says the same things through its stat tiles, which are the
    # figures a console reader actually looks at.
    tiles = dict(_HTML_TILE.findall(html))
    assert tiles["Waiting on a human"] == str(data["queue"]["open"])
    assert tiles["Gates overdue"] == str(data["gates"]["overdue"])
    assert tiles["Merges in window"] == str(data["merges"]["in_window"])
    assert tiles["Drift open"] == str(data["drift"]["open"])
    assert tiles["Median to clear"] == "2.5h", "9000s, formatted once and shown here"
    assert tiles["Oldest waiting"] == data["queue"]["oldest_waiting_age"]

    # And the derived strings, which are computed once and formatted twice.
    html_text = _HTML_TAG.sub(" ", html)
    for shared in (data["queue"]["oldest_waiting_age"], "2.5h", "abc123def456"):
        assert shared in markdown
        assert shared in html_text


def test_every_panel_row_survives_into_both_documents() -> None:
    data = _assemble()
    markdown = render_dashboard_markdown(data)
    html_text = _HTML_TAG.sub(" ", render_dashboard_html(data))
    for token in ("Factual", "Control", "sme@example.com", "Succeeded", "Triaged"):
        assert token in markdown, f"{token} missing from the Markdown"
        assert token in html_text, f"{token} missing from the HTML"


# ---------------------------------------------------------------------------
# Escaping
# ---------------------------------------------------------------------------


def test_a_hostile_reviewer_principal_cannot_inject_markup() -> None:
    data = _assemble(
        reviewers=[
            {
                "principal": HOSTILE_PRINCIPAL,
                "approvals": 1,
                "rejections": 0,
                "changes_requested": 0,
                "abstentions": 0,
                "decisions": 1,
                "last_decided_at": None,
            }
        ]
    )
    html = render_dashboard_html(data)
    assert "<script>" not in html
    assert "&lt;script&gt;" in html
    assert "alert(&quot;xss&quot;)" in html, "quotes escape too -- this lands in attributes"


def test_a_hostile_engagement_id_cannot_inject_markup() -> None:
    """The engagement id reaches the scope line and the SVG's aria-label."""
    html = render_dashboard_html(_assemble(engagement_id='" onload="alert(1)'))
    assert 'onload="alert(1)"' not in html
    assert "&quot;" in html


def test_an_unknown_status_is_escaped_in_the_bar_label() -> None:
    """Segment labels come from data and reach an `aria-label` attribute."""
    html = render_dashboard_html(
        _assemble(drift=[{"state": '<img src=x onerror="alert(1)">', "n": 1}])
    )
    assert "<img" not in html
    assert "&lt;img" in html


# ---------------------------------------------------------------------------
# The empty database
# ---------------------------------------------------------------------------


def test_an_empty_database_renders_sentences_not_blank_tables() -> None:
    data = _empty()
    markdown = render_dashboard_markdown(data)
    html = render_dashboard_html(data)

    for phrase in (
        "No proposals on record",
        "No drift signals recorded",
        "No workflow runs started",
        "No agent launches requested",
    ):
        assert phrase in markdown, f"missing empty-state sentence: {phrase}"
    assert "No proposals on record" in html
    assert "nothing outstanding" in markdown, "no open gate means no next due date"


def test_the_empty_dashboard_still_carries_its_disclosure() -> None:
    """The one thing that must never be conditional on there being data."""
    assert "not a pinned artefact" in render_dashboard_markdown(_empty())


# ---------------------------------------------------------------------------
# Shape
# ---------------------------------------------------------------------------


def test_markdown_ends_in_exactly_one_newline() -> None:
    rendered = render_dashboard_markdown(_assemble())
    assert rendered.endswith("\n")
    assert not rendered.endswith("\n\n")


def test_the_html_is_self_contained() -> None:
    """No external asset, no script. It has to render from a vault folder,
    an email client and inside the console alike."""
    html = render_dashboard_html(_assemble())
    assert "<script" not in html.lower()
    assert "http://" not in html
    assert "https://" not in html
    assert "<style>" in html, "the CSS travels with the document"
    assert 'class="fde-dash"' in html, "everything is namespaced under one scope"


def test_the_html_declares_dark_mode_both_ways() -> None:
    """The OS setting and an explicit theme toggle must each win."""
    html = render_dashboard_html(_assemble())
    assert "prefers-color-scheme: dark" in html.replace(
        "prefers-color-scheme:dark", "prefers-color-scheme: dark"
    )
    assert '[data-theme="dark"]' in html


def test_the_title_can_be_suppressed_for_a_host_page_that_has_one() -> None:
    """The console gives the page its own <h1>; the section repeating it a
    few lines below reads as a template bug."""
    titled = render_dashboard_html(_assemble(), titled=True)
    untitled = render_dashboard_html(_assemble(), titled=False)
    assert "<h2>Decision dashboard</h2>" in titled
    assert "<h2>Decision dashboard</h2>" not in untitled
    # The scope line carries `generated_at`, which no host page supplies.
    assert "Scope:" in untitled
    assert NOW.isoformat() in untitled


def test_a_zero_count_keeps_its_place_in_the_legend() -> None:
    """A status at zero must read as zero, not as absent -- so it leaves the
    bar (which would be a zero-width rect) but stays in the legend."""
    html = render_dashboard_html(_assemble(gates={**_rows()["gates"], "overdue": 0}))
    assert "Overdue 0" in html
