"""Rollout/MCP retrieval parity -- the test `rollout_env.py`'s module
docstring promised.

`rollout_env.py` deliberately re-implements `fde_mcp.tools.graph`'s SQL
instead of importing it (that module's docstring explains the concurrency
reason). The cost of that decision is drift: if the MCP server's query
changes and the rollout's does not, the policy is trained against a
retrieval production no longer serves, and nothing fails -- the training
curves look fine while the environment silently diverges.

Two mechanisms, in ascending cost:

1. **SQL text parity**, no database, runs on every PR. The literal SQL is
   pulled out of each function's source and compared after whitespace
   normalisation. The pairs that must be IDENTICAL are asserted identical,
   and -- just as important -- the pairs that must DIFFER are asserted to
   differ in exactly the documented way. Asserting the divergences too is
   what makes an accidental "fix" (someone dropping the rollout's as-of
   pinning to make this test pass) fail rather than pass.

2. **Behavioural parity**, `requires_db`. The two `kg_search`
   implementations are run against the same seeded graph with the same
   query vector and their result rows compared. Text equality cannot catch
   a divergence in how the two callers bind parameters; this can.
"""

from __future__ import annotations

import ast
import asyncio
import inspect
import re
import textwrap
from collections.abc import Callable
from typing import Any

import pytest

from fde_mcp import db as mcp_db
from fde_mcp import embeddings
from fde_mcp.tools import graph as mcp_graph
from fde_training import rollout_env

# A SQL literal here always SELECTs against a `kg.` object. Requiring both
# markers is what keeps docstrings out -- several of these functions
# describe `kg.dependency_closure` in prose, and matching on `kg.` alone
# would compare tool descriptions rather than queries.
_SQL_MARKER = re.compile(r"\bSELECT\b.*\bkg\.", re.IGNORECASE | re.DOTALL)


def _extract_sql(fn: Callable[..., Any]) -> str:
    """Concatenate the SQL string literals in `fn`'s source, normalised.

    Both modules build their queries as plain literals (no ORM, no string
    building) precisely so this is possible -- see
    `docs/11-python-conventions.md` §6 on `fde_mcp` being a thin typed
    wrapper over `db/008_retrieval.sql`.

    Parsed with `ast` rather than scanned with a regex for two reasons:
    Python merges implicitly concatenated adjacent literals for us (the
    closure/radius queries are written that way), and the docstring is
    identifiable as a node rather than guessed at by delimiter.
    """
    tree = ast.parse(textwrap.dedent(inspect.getsource(fn)))
    docstrings = {
        ast.get_docstring(node, clean=False)
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Module))
    }
    parts = [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and node.value not in docstrings
        and _SQL_MARKER.search(node.value)
    ]
    if not parts:
        msg = f"no SQL literal found in {fn.__qualname__} -- did the query stop being a literal?"
        raise AssertionError(msg)
    return _normalize(" ".join(parts))


def _normalize(sql: str) -> str:
    """Collapse formatting differences that are not drift.

    The two modules wrap their argument lists differently (the MCP tools
    put the opening paren at the end of a line), and a test that failed on
    that would be a test people learn to ignore. Token-level differences
    survive normalisation; whitespace ones do not.
    """
    sql = re.sub(r"\s+", " ", sql).strip()
    sql = re.sub(r"\(\s+", "(", sql)
    sql = re.sub(r"\s+\)", ")", sql)
    return re.sub(r"\s+,", ",", sql)


# Pairs whose SQL must be byte-identical after whitespace normalisation.
IDENTICAL_PAIRS = [
    ("kg_search", rollout_env.RolloutEnv.kg_search, mcp_graph.kg_search),
    ("kg_lexical_search", rollout_env.RolloutEnv.kg_lexical_search, mcp_graph.kg_lexical_search),
    ("kg_process_flow", rollout_env.RolloutEnv.kg_process_flow, mcp_graph.kg_process_flow),
]


@pytest.mark.parametrize(("name", "rollout_fn", "mcp_fn"), IDENTICAL_PAIRS)
def test_rollout_sql_matches_the_mcp_tool(
    name: str, rollout_fn: Callable[..., Any], mcp_fn: Callable[..., Any]
) -> None:
    assert _extract_sql(rollout_fn) == _extract_sql(mcp_fn), (
        f"{name}: rollout_env and fde_mcp.tools.graph have drifted. The RL policy would "
        f"be trained against a retrieval production does not serve. Reconcile them, or -- "
        f"if the divergence is deliberate -- add it to the whitelist below WITH a reason."
    )


# ---------------------------------------------------------------------------
# Deliberate divergences. Each is asserted EXACTLY, so both further drift and
# silent convergence fail.
# ---------------------------------------------------------------------------
def test_traverse_diverges_only_by_the_pinned_as_of_argument() -> None:
    """The rollout pins `kg.traverse` to its episode's commit; the MCP tool
    reads live, which is correct for an agent answering a question about
    the business as it is now."""
    rollout_sql = _extract_sql(rollout_env.RolloutEnv.kg_traverse)
    mcp_sql = _extract_sql(mcp_graph.kg_traverse)
    assert "%(direction)s, 0.0, %(as_of)s)" in rollout_sql
    assert mcp_sql.endswith("%(direction)s)")
    assert "as_of" not in mcp_sql
    assert rollout_sql.replace(", 0.0, %(as_of)s", "") == mcp_sql


@pytest.mark.parametrize(
    ("rollout_fn", "mcp_fn", "sql_fn"),
    [
        (
            rollout_env.RolloutEnv.kg_dependency_closure,
            mcp_graph.kg_dependency_closure,
            "kg.dependency_closure",
        ),
        (rollout_env.RolloutEnv.kg_impact_radius, mcp_graph.kg_impact_radius, "kg.impact_radius"),
    ],
)
def test_closure_and_radius_diverge_only_by_the_as_of_overload(
    rollout_fn: Callable[..., Any], mcp_fn: Callable[..., Any], sql_fn: str
) -> None:
    """db/015_closure_asof.sql adds 4-arg as-of overloads. The rollout calls
    those; the 3-arg signatures stay the MCP tool contract. Before 015 the
    rollout called the 3-arg form and silently read `now()`, so two of its
    eight tools were not commit-pinned at all."""
    rollout_sql = _extract_sql(rollout_fn)
    mcp_sql = _extract_sql(mcp_fn)
    assert f"{sql_fn}(%(eng)s::uuid, %(key)s, %(hops)s, %(as_of)s)" in rollout_sql
    assert f"{sql_fn}(%(eng)s::uuid, %(key)s, %(hops)s)" in mcp_sql
    assert rollout_sql.replace(", %(as_of)s)", ")") == mcp_sql


def test_get_node_node_query_matches_but_the_mcp_tool_fetches_more() -> None:
    """The rollout returns the node row alone; the MCP tool also returns
    incident edges, evidence, and an aggregate strength. That is a
    deliberate result-shape difference (a rollout scores retrieval, a human-
    facing tool explains an entity) -- but the NODE query itself must still
    be the same query."""
    node_query = (
        "SELECT * FROM kg.node_current WHERE engagement_id = %(eng)s::uuid AND node_key = %(key)s"
    )
    rollout_sql = _extract_sql(rollout_env.RolloutEnv.kg_get_node)
    mcp_sql = _extract_sql(mcp_graph.kg_get_node)
    assert rollout_sql == _normalize(node_query)
    assert _normalize(node_query) in mcp_sql
    assert "kg.evidence_strength" in mcp_sql
    assert "kg.edge_current" in mcp_sql


def test_every_rollout_tool_is_covered_by_a_parity_assertion() -> None:
    """A new rollout tool with no parity coverage is exactly how drift gets
    reintroduced, so the roster is asserted rather than trusted."""
    covered = {
        "kg_search",
        "kg_lexical_search",
        "kg_process_flow",
        "kg_traverse",
        "kg_dependency_closure",
        "kg_impact_radius",
        "kg_get_node",
        # kg_propose is validated in-process and never queries the graph;
        # rollout_env deliberately never writes hitl.proposal.
        "kg_propose",
    }
    actual = {
        name
        for name in dir(rollout_env.RolloutEnv)
        if name.startswith("kg_") and callable(getattr(rollout_env.RolloutEnv, name))
    }
    assert actual == covered


# ---------------------------------------------------------------------------
# Behavioural parity
# ---------------------------------------------------------------------------
@pytest.mark.requires_db
def test_kg_search_returns_the_same_rows_through_both_paths(
    seeded_graph: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Same graph, same query vector, both implementations. Compared on the
    ranked node_key list and on rrf_score, because a parameter-binding
    divergence shows up as a different ranking long before it shows up as a
    different query."""
    engagement_id = seeded_graph["engagement_id"]
    query = "who approves a deep discount"
    vector = seeded_graph["query_vector"]

    env = rollout_env.RolloutEnv(
        engagement_id=engagement_id,
        embed_fn=lambda _text: vector,
        dsn=seeded_graph["dsn"],
        role=seeded_graph["role"],
    )
    env.reset(task_kind="parity_test", task_input={"question": query})
    rollout_result = env.kg_search(query=query, k=10)

    async def _fake_embed(text: str, **kwargs: Any) -> list[float]:
        del text, kwargs
        return vector

    monkeypatch.setattr(embeddings, "embed", _fake_embed)

    async def _run_mcp() -> dict[str, Any]:
        try:
            return await mcp_graph.kg_search(engagement_id=engagement_id, query=query, k=10)
        finally:
            await mcp_db.close_pool()

    mcp_result = asyncio.run(_run_mcp())

    rollout_rows = rollout_result["results"]
    mcp_rows = mcp_result["results"]
    assert [r["node_key"] for r in rollout_rows] == [r["node_key"] for r in mcp_rows]
    for a, b in zip(rollout_rows, mcp_rows, strict=True):
        assert a["node_type"] == b["node_type"]
        assert a["label"] == b["label"]
        assert float(a["rrf_score"]) == pytest.approx(float(b["rrf_score"]), rel=1e-9)
