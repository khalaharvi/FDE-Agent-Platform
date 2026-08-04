"""`hitl_export_dashboard` against a live database, as the role it really runs as.

The renderers are covered without a database in `test_dashboard_render.py`.
What needs one here is the half that cannot be faked: that every query runs
under `fde_agent` without tripping a grant, and that the one table that role
genuinely cannot read is reported as unreadable rather than as empty.
"""

from __future__ import annotations

import pytest

from fde_mcp.server import build_server
from fde_mcp.tools.dashboard import hitl_export_dashboard

pytestmark = pytest.mark.requires_db


async def test_the_tool_is_registered() -> None:
    names = [tool.name for tool in await build_server().list_tools()]
    assert "hitl_export_dashboard" in names


async def test_every_query_runs_under_the_agent_role(engagement_id: str) -> None:
    """The whole point of a live-DB test here.

    Eight SELECTs across four schemas, all as `fde_agent`. A missing grant
    surfaces as `{"error": ...}` through `pg_error_boundary` rather than as an
    exception, so an assertion on the happy shape is what catches it.
    """
    result = await hitl_export_dashboard(engagement_id=engagement_id)

    assert "error" not in result, result.get("error")
    assert set(result) == {
        "engagement_id",
        "window_days",
        "generated_at",
        "markdown",
        "html",
    }
    assert result["engagement_id"] == engagement_id
    assert result["window_days"] == 30
    assert result["markdown"].startswith("---\n")
    assert "# Decision dashboard" in result["markdown"]
    assert 'class="fde-dash"' in result["html"]


async def test_the_launches_panel_reports_the_grant_not_an_empty_table(
    engagement_id: str,
) -> None:
    """db/018 withholds `wf.agent_launch` from `fde_agent`, and CI asserts the
    write denials. The read is denied by the same omission, so this tool can
    never show launches -- and must not let that read as "no agents ran".
    """
    result = await hitl_export_dashboard(engagement_id=engagement_id)

    for rendered in (result["markdown"], result["html"]):
        assert "wf.agent_launch" in rendered
        assert "db/018" in rendered
        assert "not a statement that no agents ran" in rendered
        assert "No agent launches requested" not in rendered


async def test_the_reviewer_panel_is_readable_by_this_role(engagement_id: str) -> None:
    """`fde_agent` DOES hold SELECT on `hitl.reviewer` (db/011:53), unlike the
    console's prodops role -- so this panel must carry data, not the notice."""
    result = await hitl_export_dashboard(engagement_id=engagement_id)
    assert "cannot see hitl.reviewer" not in result["markdown"]


async def test_the_rollup_needs_no_engagement(engagement_id: str) -> None:
    result = await hitl_export_dashboard()
    assert "error" not in result
    assert result["engagement_id"] is None
    assert "all engagements" in result["markdown"]


async def test_the_window_is_clamped_rather_than_refused(engagement_id: str) -> None:
    """A model asking for 3650 days wants "everything". Refusing the call
    teaches it less than giving it the widest window and saying so."""
    assert (await hitl_export_dashboard(engagement_id, window_days=3650))["window_days"] == 365
    assert (await hitl_export_dashboard(engagement_id, window_days=0))["window_days"] == 1


async def test_the_result_declares_that_it_is_not_pinned(engagement_id: str) -> None:
    """The tool next door guarantees byte-stability; this one must not be
    mistaken for it."""
    result = await hitl_export_dashboard(engagement_id=engagement_id)
    assert "pinned: false" in result["markdown"]
    assert "not a pinned artefact" in result["markdown"]


async def test_the_docstring_warns_against_diffing_two_exports() -> None:
    """The docstring is the model-facing prompt (docs/11 §6). The anti-
    stability disclosure is the part a model will otherwise get wrong, having
    just read wf_export_playbook's opposite promise."""
    doc = hitl_export_dashboard.__doc__ or ""
    assert "NOT stable" in doc
    assert "wf_export_playbook" in doc
    assert "Read-only" in doc
