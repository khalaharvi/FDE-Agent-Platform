"""fde_mcp.tools -- the 21 MCP tool implementations, grouped by concern.

`register_all` is the single seam between "a FastMCP instance exists" and
"it has tools on it" -- server.py calls it once and does not know or care
how many modules exist underneath. Splitting graph/proposals/drift/workflow/
evidence into their own modules (rather than one file) means a change to
drift triage cannot accidentally touch graph retrieval in the same diff,
and each module's test file maps 1:1 to the tools it covers.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from fde_mcp.tools import drift, evidence, graph, proposals, workflow

if TYPE_CHECKING:
    from mcp.server.fastmcp import FastMCP

__all__ = ["register_all"]


def register_all(mcp: FastMCP[None]) -> None:
    """Register all 21 tools (9 graph, 3 proposal, 3 drift, 3 workflow, 3 evidence)."""
    graph.register(mcp)
    proposals.register(mcp)
    drift.register(mcp)
    workflow.register(mcp)
    evidence.register(mcp)
