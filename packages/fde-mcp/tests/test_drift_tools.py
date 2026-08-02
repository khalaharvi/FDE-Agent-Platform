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


async def test_drift_scan_fails_gracefully_not_loudly(seed: dict[str, Any]) -> None:
    """sor.run_all_detectors() REFRESHes a materialized view CONCURRENTLY,
    which requires matview ownership Postgres 16 has no way to grant --
    verified directly against fde_ingest, the intended role, not just
    fde_agent. drift_scan must not crash the server for this; it returns a
    structured error instead (see docs/08-drift-monitor.md's "Known
    limitations" section).
    """
    result = await drift.drift_scan(engagement_id=seed["engagement_id"])
    if "error" in result:
        assert "hint" in result
    else:
        assert "summary" in result  # in case a future environment grants ownership


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
