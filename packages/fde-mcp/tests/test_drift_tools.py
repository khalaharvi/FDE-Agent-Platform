"""Tests for fde_mcp.tools.drift -- drift monitoring against systems of record."""

from __future__ import annotations

from typing import Any

import pytest

from fde_mcp.server import mcp
from fde_mcp.tools import drift

pytestmark = pytest.mark.requires_db


async def test_drift_list_empty_is_fine(seed: dict[str, Any]) -> None:
    result = await drift.drift_list(
        engagement_id=seed["engagement_id"], state="open", min_severity="low"
    )
    assert result["returned"] == 0
    assert result["signals"] == []


async def test_drift_scan_succeeds_as_fde_agent(seed: dict[str, Any]) -> None:
    """sor.run_all_detectors() is SECURITY DEFINER precisely so the matview
    REFRESH CONCURRENTLY ownership requirement is satisfied regardless of the
    calling role, and db/011 grants fde_agent EXECUTE on it (docs/99-sources.md
    §7/§8 record both fixes). Against the shipped schema this call therefore
    MUST succeed -- an earlier version of this test accepted an error branch
    too, which made it impossible to fail and hid exactly the silent-monitoring
    regression it exists to catch.
    """
    result = await drift.drift_scan(engagement_id=seed["engagement_id"])
    assert "error" not in result, result
    assert "summary" in result


async def test_drift_triage_rejects_resolved_state(seed: dict[str, Any]) -> None:
    with pytest.raises(Exception):  # noqa: B017, PT011 -- mirrors the tool's own broad ToolError contract
        await mcp.call_tool(
            "drift_triage",
            {
                "engagement_id": seed["engagement_id"],
                "signal_id": 1,
                "state": "resolved",
                "note": "nope",
            },
        )


async def test_drift_triage_not_found(seed: dict[str, Any]) -> None:
    result = await drift.drift_triage(
        engagement_id=seed["engagement_id"],
        signal_id=999_999_999,
        state="dismissed",
        note="no such signal",
    )
    assert "error" in result
