"""Tests for fde_mcp.tools.graph -- the read-only knowledge-graph tools.

Run against a live Postgres with db/ applied (see conftest.py's `seed`
fixture for the exact graph it builds). `kg_search`'s embedding call is
monkeypatched to a deterministic local vector (conftest.py's
`_fake_bedrock`), so pgvector HNSW search, RRF fusion, and graph expansion
are all exercised for real -- only the Bedrock network call is stubbed.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from mcp.server.fastmcp.exceptions import ToolError

from fde_mcp.server import mcp
from fde_mcp.tools import graph

pytestmark = pytest.mark.requires_db


# ===========================================================================
# kg_head_commit / kg_as_of
# ===========================================================================
async def test_kg_head_commit_returns_seeded_commit(seed: dict[str, Any]) -> None:
    result = await graph.kg_head_commit(engagement_id=seed["engagement_id"])
    assert result["has_head"] is True
    assert result["commit_id"] == seed["commit_id"]
    assert result["commit_uuid"] == seed["commit_uuid"]


async def test_kg_head_commit_unknown_engagement_has_no_head() -> None:
    result = await graph.kg_head_commit(engagement_id=str(uuid.uuid4()))
    assert result["has_head"] is False
    assert result["commit_id"] is None


async def test_kg_as_of_reports_no_diff_for_an_up_to_date_snapshot(seed: dict[str, Any]) -> None:
    result = await graph.kg_as_of(engagement_id=seed["engagement_id"], at="2999-01-01T00:00:00Z")
    assert "error" not in result
    assert result["node_count_as_of"] == result["node_count_head"]
    assert result["diff_vs_head"]["nodes_added_since_count"] == 0
    assert result["diff_vs_head"]["nodes_removed_since_count"] == 0


# ===========================================================================
# kg_search -- REQUIRED: must return provenance
# ===========================================================================
async def test_kg_search_returns_provenance(seed: dict[str, Any]) -> None:
    result = await graph.kg_search(
        engagement_id=seed["engagement_id"], query="who approves large discounts", k=5
    )
    assert "error" not in result
    assert result["returned"] > 0
    for row in result["results"]:
        assert "provenance" in row
        assert isinstance(row["provenance"], dict)
        assert row["provenance"], "provenance must not be empty"
        assert "evidence_strength" in row["provenance"]
        assert "lists_matched" in row["provenance"]

    keys = [r["node_key"] for r in result["results"]]
    assert "act.discount_approval" in keys, (
        "the fake embed() returns a vector identical to this node's seeded "
        "embedding; it must be the top (or at least present) node-ANN hit"
    )
    top = next(r for r in result["results"] if r["node_key"] == "act.discount_approval")
    assert "node_ann" in top["provenance"], (
        "expected a node_ann provenance entry for an exact-vector match"
    )


async def test_kg_search_node_type_filter(seed: dict[str, Any]) -> None:
    result = await graph.kg_search(
        engagement_id=seed["engagement_id"], query="discount approval", k=10, node_types=["control"]
    )
    assert "error" not in result
    for row in result["results"]:
        assert row["node_type"] == "control"


# ===========================================================================
# kg_lexical_search
# ===========================================================================
async def test_kg_lexical_search_finds_exact_label(seed: dict[str, Any]) -> None:
    result = await graph.kg_lexical_search(
        engagement_id=seed["engagement_id"], text="Discount Approval"
    )
    assert result["returned"] >= 1
    assert any(r["node_key"] == "act.discount_approval" for r in result["results"])


# ===========================================================================
# kg_get_node
# ===========================================================================
async def test_kg_get_node_groups_edges_and_evidence(seed: dict[str, Any]) -> None:
    result = await graph.kg_get_node(
        engagement_id=seed["engagement_id"], node_key="act.discount_approval"
    )
    assert result["node"]["node_key"] == "act.discount_approval"
    assert "gated_by" in result["edges"]["outgoing"]
    assert result["edges"]["outgoing"]["gated_by"][0]["neighbor_key"] == "ctrl.discount_threshold"
    assert "performs" in result["edges"]["incoming"]
    assert result["edges"]["incoming"]["performs"][0]["neighbor_key"] == "role.deal_desk"
    assert "evidence_strength" in result


async def test_kg_get_node_not_found(seed: dict[str, Any]) -> None:
    result = await graph.kg_get_node(engagement_id=seed["engagement_id"], node_key="does.not.exist")
    assert "error" in result


# ===========================================================================
# kg_traverse -- REQUIRED: respects max_hops ceiling
# ===========================================================================
async def test_kg_traverse_follows_edges(seed: dict[str, Any]) -> None:
    result = await graph.kg_traverse(
        engagement_id=seed["engagement_id"],
        start_keys=["act.discount_approval"],
        max_hops=2,
        direction="out",
    )
    keys = {n["node_key"] for n in result["nodes"]}
    assert "ctrl.discount_threshold" in keys
    assert "sys.salesforce_cpq" in keys


async def test_kg_traverse_db_enforces_hard_hop_ceiling(seed: dict[str, Any]) -> None:
    """kg.traverse itself RAISEs above 6 hops (008_retrieval.sql), regardless
    of how the tool is invoked -- calling the underlying function directly
    (bypassing the MCP/pydantic argument schema) still hits this ceiling.
    """
    with pytest.raises(RuntimeError) as exc_info:
        await graph.kg_traverse(
            engagement_id=seed["engagement_id"], start_keys=["act.discount_approval"], max_hops=7
        )
    assert "max_hops" in str(exc_info.value) or "ceiling" in str(exc_info.value)


async def test_kg_traverse_schema_rejects_hop_ceiling_via_mcp_protocol(
    seed: dict[str, Any],
) -> None:
    """The MCP-visible tool schema also rejects out-of-range max_hops before
    a query is ever sent to the database (defense in depth).
    """
    with pytest.raises(ToolError):
        await mcp.call_tool(
            "kg_traverse",
            {
                "engagement_id": seed["engagement_id"],
                "start_keys": ["act.discount_approval"],
                "max_hops": 7,
            },
        )


async def test_kg_traverse_schema_rejects_max_nodes_ceiling_via_mcp_protocol(
    seed: dict[str, Any],
) -> None:
    with pytest.raises(ToolError):
        await mcp.call_tool(
            "kg_traverse",
            {
                "engagement_id": seed["engagement_id"],
                "start_keys": ["act.discount_approval"],
                "max_nodes": 5001,
            },
        )


# ===========================================================================
# kg_dependency_closure / kg_impact_radius / kg_process_flow
# ===========================================================================
async def test_kg_dependency_closure(seed: dict[str, Any]) -> None:
    result = await graph.kg_dependency_closure(
        engagement_id=seed["engagement_id"], node_key="act.discount_approval"
    )
    keys = {n["node_key"] for n in result["closure"]}
    assert "sys.salesforce_cpq" in keys
    assert "ctrl.discount_threshold" in keys


async def test_kg_impact_radius(seed: dict[str, Any]) -> None:
    result = await graph.kg_impact_radius(
        engagement_id=seed["engagement_id"], node_key="ctrl.discount_threshold"
    )
    keys = {n["node_key"] for n in result["impact"]}
    assert "act.discount_approval" in keys


async def test_kg_process_flow_orders_activities(seed: dict[str, Any]) -> None:
    result = await graph.kg_process_flow(
        engagement_id=seed["engagement_id"], process_key="proc.quote_to_cash"
    )
    keys_in_order = [s["activity_key"] for s in result["steps"]]
    assert keys_in_order.index("act.legal_review") < keys_in_order.index("act.discount_approval")
