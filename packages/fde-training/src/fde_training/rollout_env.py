"""rollout_env.py -- the live RL environment: real MCP tool surface, real
Postgres, safe for concurrent rollouts.

Why this does not simply import fde_mcp.server's tool functions
------------------------------------------------------------------
`fde_mcp.server`'s module docstring says the design goal explicitly: "If
the retrieval an RL rollout sees differs from what production serves, the
policy you train is optimising a different environment than the one it
will be deployed into." The most literal way to honour that would be to
import `kg_search`, `kg_traverse`, etc. directly from `fde_mcp.server`
(they are plain, directly-callable `async def`s -- `@mcp.tool()` in the
installed `mcp` SDK returns the original function unchanged). We
deliberately do NOT do that here, for one concrete, concurrency-breaking
reason: `fde_mcp.server`'s tracing reads process configuration
(`fde_mcp.config.get_settings().agent.trace_session_id`) captured once and
shared process-wide. Under N concurrent RL rollouts running as asyncio
tasks (or, here, threads via a shared sync pool) in one process -- exactly
the scenario this module exists to support -- every task would share that
one global; there is no way to point two concurrent rollouts' tool calls at
two different `trn.trace_session` rows without a data race. So instead,
this module re-implements the SAME SQL queries `fde_mcp.server`'s tools run
(copied 1:1 from db/008_retrieval.sql, cited per-method below) against an
explicit, per-episode `session_id` threaded through every call --
byte-identical retrieval behaviour, safe concurrency. If `fde_mcp.server`'s
queries change, these must change with them, and that is enforced rather
than remembered: `tests/test_parity.py` diffs the two modules' SQL text on
every run and asserts the small set of deliberate divergences (commit
pinning, `kg_get_node`'s richer MCP-side result) exactly, so both silent
drift and silent convergence fail the build.

Reproducibility: commit pinning
---------------------------------
`reset()` reads the current sealed HEAD commit (`kg.commit`) and pins
`(commit_id, sealed_at)` for the life of the episode. Every subsequent call
to an as-of-capable function is pinned to that `sealed_at` timestamp, so
re-running the episode later (for debugging a reward, or replaying it for
SFT export) sees the same graph even if new commits have since merged.

Three functions are as-of-capable, and only two of them always were.
`kg.traverse` has taken `p_as_of` since db/008_retrieval.sql.
`kg.dependency_closure`/`kg.impact_radius` shipped as 3-arg wrappers that
called `kg.traverse` WITHOUT it -- so they always read `now()`, and two of
this module's eight tools were silently not commit-pinned while this
docstring claimed otherwise. The migration adding 4-arg
`(engagement, key, max_hops, as_of)` overloads is what makes the claim
true; the 3-arg signatures remain untouched because they are the MCP tool
contract (`fde_mcp.tools.graph`), where reading live is the correct
behaviour -- an agent answering a question about the business should see
the business as it is now. Only the rollout pins.

HONEST LIMITATION, not papered over: `kg.hybrid_search` / `kg.ann_nodes` /
`kg.ann_edges` / `kg.ann_chunks` (db/008_retrieval.sql) have NO `p_as_of`
parameter -- they always read `is_current`/`node_current` rows. True
point-in-time reproducibility for ANN retrieval is not something the schema
exposes (it would need MVCC snapshot pinning, not a timestamp filter). This
environment's `step()` therefore checks, before every ANN-backed call
(`kg_search`, `kg_lexical_search`), that HEAD has not advanced past the
pinned commit (`_assert_commit_unchanged`) and raises a clear
`StaleCommitError` rather than silently returning results from a graph
state the episode was not pinned to. In a long-running training job against
a graph that is concurrently being merged into, this will occasionally
abort an in-flight episode -- that is the correct behaviour (a silently
non-reproducible episode is a worse outcome for RL, where credit assignment
already depends on faithfully replaying what the policy saw).

Concurrency and the DB role
-----------------------------
Built on `psycopg_pool.ConnectionPool` (sync; matches the sync `step()`
interface TRL's `environment_factory` pattern implies for a plain stateful
class). Every tool call and every trace write happens inside ONE short
transaction with `SET LOCAL ROLE fde_rl_rollout` (see `_RoleScopedTransaction`
below), so N `RolloutEnv` instances sharing one process-wide pool never
elevate a connection beyond the rollout's actual privileges, and a bug in
one episode's tool call cannot write to another episode's session
(`session_id` is a parameter on every query, never a shared mutable
attribute read by the query itself).

`fde_rl_rollout` (`db/012_rl_rollout_role.sql`) is a role narrower than
`fde_agent`: SELECT + EXECUTE on `kg.*`, INSERT/SELECT/DELETE on
`trn.trace_session`/`trn.trace_step` ONLY -- no write access to `hitl.*`
at all (unlike `fde_agent`, which the MCP server uses and which CAN write
`hitl.proposal`/`proposal_item`). This is the "read-only role" an RL
rollout needs: it must never be able to actually stage a real proposal a
human reviewer might see in their queue. `propose()` below therefore
VALIDATES a proposal-shaped action (same schema checks
`fde_training.rewards.r_schema_valid` uses) and returns it as the episode's
terminal action for the reward functions to score, without ever touching
`hitl.proposal`.

`ensure_rollout_role`'s inline DDL is now a strict SUBSET of what
`db/012_rl_rollout_role.sql`'s real migration grants (that migration exists
in this repo; this bootstrap predates it and is kept only as a best-effort,
idempotent safety net for a database the migration has not yet been applied
to -- production deployments should rely on the migration, not this
function).
"""

from __future__ import annotations

import hashlib
import json
import math
import time
import uuid as uuid_mod
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import psycopg
from psycopg import sql
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

from fde_mcp.logging import get_logger
from fde_training import common
from fde_training.config import get_settings
from fde_training.rewards.validity import validate_tool_call_args

log = get_logger(__name__)

ENSURE_ROLE_SQL = """
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'fde_rl_rollout') THEN
    CREATE ROLE fde_rl_rollout NOLOGIN;
  END IF;
END $$;
GRANT USAGE ON SCHEMA kg, trn TO fde_rl_rollout;
GRANT SELECT ON ALL TABLES IN SCHEMA kg TO fde_rl_rollout;
GRANT EXECUTE ON ALL FUNCTIONS IN SCHEMA kg TO fde_rl_rollout;
GRANT INSERT, SELECT, DELETE ON trn.trace_session, trn.trace_step TO fde_rl_rollout;
-- Matches db/012_rl_rollout_role.sql exactly. `outcome` is NOT here: it is
-- what trn.sft_export filters on, so a rollout able to set it could inject
-- its own exploration into the supervised set as human-approved data.
GRANT UPDATE (ended_at, final_output, total_tokens, latency_ms) ON trn.trace_session TO fde_rl_rollout;
REVOKE UPDATE (outcome, label_proposal_id, label_source, split) ON trn.trace_session FROM fde_rl_rollout;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA trn TO fde_rl_rollout;
-- Explicitly denied, mirroring db/010_roles_and_seed_policy.sql's fde_agent
-- posture but stricter: this role never gets hitl.* at all.
REVOKE ALL ON ALL TABLES IN SCHEMA hitl FROM fde_rl_rollout;
"""


def ensure_rollout_role(dsn: str | None = None) -> bool:
    """Best-effort idempotent role bootstrap. Returns True if it succeeded,
    False if the connecting principal lacks CREATE ROLE / GRANT privileges
    (expected in a locked-down production DB -- an admin should have applied
    `db/012_rl_rollout_role.sql` instead). Never raises: a missing role
    surfaces later as a normal, actionable `InsufficientPrivilege` on the
    first tool call, same as any other misconfigured grant in this
    codebase.
    """
    try:
        with psycopg.connect(dsn or common.resolve_dsn(), autocommit=True) as conn:
            conn.execute(ENSURE_ROLE_SQL)
        return True
    except psycopg.Error:
        log.warning(
            "rollout_role_bootstrap_failed",
            role=get_settings().roles.rollout_role,
            hint="if this is production, rely on db/012_rl_rollout_role.sql instead",
        )
        return False


class StaleCommitError(RuntimeError):
    """Raised when an ANN-backed retrieval call is attempted after HEAD has
    advanced past the episode's pinned commit -- see module docstring's
    "HONEST LIMITATION" section."""


class ToolBudgetExceededError(RuntimeError):
    """Raised by `step()` once the per-episode tool-call budget is spent."""


EmbedFn = Callable[[str], list[float]]


def deterministic_fake_embed(text: str, dims: int = 1024) -> list[float]:
    """A dependency-free stand-in for `fde_mcp.embeddings.embed`, using the
    same deterministic construction as the training fixture data
    (`sin(i*0.013 + hash(text)/1e9)`) so unit/integration tests here produce
    the exact same vectors `kg.ann_nodes` was seeded with, with zero network
    egress and zero AWS credentials. NEVER use this in a real rollout against
    production data -- it carries no semantic information, it only exists so
    this module is independently testable. See `default_embed_fn` for the
    real path.
    """
    h = int(hashlib.sha256(text.encode("utf-8")).hexdigest()[:8], 16)
    return [math.sin(i * 0.013 + h / 1e9) for i in range(1, dims + 1)]


def default_embed_fn(text: str) -> list[float]:
    """Real embedding path: shells out to `fde_mcp.embeddings`'s Bedrock
    client (Titan/Cohere, see that module) via a one-off event loop. Kept
    out of the hot import path (imported lazily inside this function) so
    `import fde_training.rollout_env` never requires `boto3`/network access
    -- only constructing a `RolloutEnv` with `embed_fn=default_embed_fn`
    (the default) and then actually calling `kg_search` does.
    """
    import asyncio  # noqa: PLC0415 -- see module docstring's heavy-import policy

    from fde_mcp import embeddings  # noqa: PLC0415

    return asyncio.run(embeddings.embed(text, input_type="search_query"))


def _to_pgvector_literal(vec: list[float]) -> str:
    return "[" + ",".join(repr(float(x)) for x in vec) + "]"


@dataclass
class StepRecord:
    turn: int
    tool_name: str
    arguments: dict[str, Any]
    result: Any
    retrieval: dict[str, Any] | None
    latency_ms: float


@dataclass
class EpisodeState:
    session_id: str
    engagement_id: str
    pinned_commit_id: int
    pinned_sealed_at: Any
    task_kind: str
    started_monotonic: float
    steps: list[StepRecord] = field(default_factory=list)
    finished: bool = False


class RolloutEnv:
    """Stateful RL environment over the real FDE knowledge graph. One
    instance per episode (cheap to construct; the connection POOL is shared
    process-wide via `RolloutEnv.shared_pool()`).

    Usage (TRL `environment_factory` shape)::

        env = RolloutEnv(engagement_id=eng, tool_budget=12)
        obs = env.reset(task_kind="map_workflow", task_input={"question": q})
        result = env.step("kg_search", {"query": q, "k": 10})
        ...
        env.finish(final_answer="...", outcome="pending")

    Usage (TRL `tools=[...]` shape): see `as_tool_functions`.
    """

    _pool: ConnectionPool | None = None

    def __init__(
        self,
        engagement_id: str,
        *,
        role: str | None = None,
        tool_budget: int = 12,
        embed_fn: EmbedFn = default_embed_fn,
        dsn: str | None = None,
        agent_name: str = "engagement",
        model_id: str = "rl-rollout",
        policy_version: str = "grpo-dev",
    ) -> None:
        self.engagement_id = engagement_id
        self.role = role if role is not None else get_settings().roles.rollout_role
        self.tool_budget = tool_budget
        self.embed_fn = embed_fn
        self.dsn = dsn or common.resolve_dsn()
        self.agent_name = agent_name
        self.model_id = model_id
        self.policy_version = policy_version
        self.state: EpisodeState | None = None

    # -- pool -----------------------------------------------------------
    @classmethod
    def shared_pool(
        cls, dsn: str | None = None, min_size: int = 1, max_size: int = 16
    ) -> ConnectionPool:
        if cls._pool is None:
            cls._pool = ConnectionPool(
                conninfo=dsn or common.resolve_dsn(),
                min_size=min_size,
                max_size=max_size,
                kwargs={"row_factory": dict_row},
                open=True,
            )
        return cls._pool

    def _tool_transaction(self) -> _RoleScopedTransaction:
        pool = self.shared_pool(self.dsn)
        return _RoleScopedTransaction(pool, self.role)

    # -- lifecycle --------------------------------------------------------
    def reset(
        self, task_kind: str, task_input: dict[str, Any], session_id: str | None = None
    ) -> dict[str, Any]:
        session_id = session_id or str(uuid_mod.uuid4())
        with self._tool_transaction() as conn, conn.cursor() as cur:
            cur.execute(
                """SELECT commit_id, sealed_at FROM kg.commit
                        WHERE engagement_id = %(eng)s AND status = 'sealed'
                        ORDER BY commit_id DESC LIMIT 1""",
                {"eng": self.engagement_id},
            )
            row = cur.fetchone()
            if row is None:
                msg = (
                    f"engagement {self.engagement_id} has no sealed commit yet -- "
                    f"nothing to reason over. Seed the graph first."
                )
                raise RuntimeError(msg)
            pinned_commit_id, pinned_sealed_at = row["commit_id"], row["sealed_at"]

            cur.execute(
                """INSERT INTO trn.trace_session
                        (session_id, engagement_id, agent_name, agent_runtime_arn, model_id,
                         policy_version, base_commit_id, task_kind, task_input, outcome)
                       VALUES (%(sid)s,%(eng)s,%(agent)s,%(arn)s,%(model)s,%(pv)s,%(commit)s,
                               %(task_kind)s,%(task_input)s,'pending')""",
                {
                    "sid": session_id,
                    "eng": self.engagement_id,
                    "agent": self.agent_name,
                    "arn": "training:rollout_env",
                    "model": self.model_id,
                    "pv": self.policy_version,
                    "commit": pinned_commit_id,
                    "task_kind": task_kind,
                    "task_input": Jsonb(task_input),
                },
            )
        self.state = EpisodeState(
            session_id=session_id,
            engagement_id=self.engagement_id,
            pinned_commit_id=pinned_commit_id,
            pinned_sealed_at=pinned_sealed_at,
            task_kind=task_kind,
            started_monotonic=time.monotonic(),
        )
        log.info(
            "episode_reset",
            session_id=session_id,
            engagement_id=self.engagement_id,
            pinned_commit_id=pinned_commit_id,
        )
        return {"session_id": session_id, "commit_id": pinned_commit_id, "task_input": task_input}

    def _require_state(self) -> EpisodeState:
        if self.state is None:
            msg = "reset() must be called before step()/finish()"
            raise RuntimeError(msg)
        return self.state

    def _assert_commit_unchanged(self, conn: psycopg.Connection[Any]) -> None:
        st = self._require_state()
        with conn.cursor() as cur:
            cur.execute(
                """SELECT commit_id FROM kg.commit
                    WHERE engagement_id = %(eng)s AND status = 'sealed'
                    ORDER BY commit_id DESC LIMIT 1""",
                {"eng": self.engagement_id},
            )
            row = cur.fetchone()
        current_head = row["commit_id"] if row else None
        if current_head != st.pinned_commit_id:
            msg = (
                f"episode {st.session_id} pinned commit {st.pinned_commit_id} but HEAD is "
                f"now {current_head}; kg.hybrid_search/kg.lexical_search have no as-of "
                f"parameter (see module docstring) so this episode can no longer "
                f"reproducibly retrieve. Abort and re-sample this episode from the current HEAD."
            )
            raise StaleCommitError(msg)

    # -- the tool surface ---------------------------------------------------
    # Every method below mirrors ONE fde_mcp.server tool's SQL 1:1. See the
    # module docstring for why this is a deliberate re-implementation, not
    # an import.
    def kg_search(
        self, query: str, k: int = 20, node_types: list[str] | None = None, expand_hops: int = 2
    ) -> dict[str, Any]:
        """Hybrid ANN + graph retrieval (mirrors fde_mcp.server:kg_search /
        kg.hybrid_search, db/008_retrieval.sql)."""
        vec = self.embed_fn(query)
        sql_text = """
            SELECT node_key, node_type, label, summary, rrf_score, provenance
              FROM kg.hybrid_search(%(eng)s::uuid, %(vec)s::kg.embedding, %(k)s, %(seed_k)s,
                                     %(hops)s, NULL::kg.edge_type[], %(node_types)s::kg.node_type[], 60)
        """
        params = {
            "eng": self.engagement_id,
            "vec": _to_pgvector_literal(vec),
            "k": k,
            "seed_k": max(30, k),
            "hops": expand_hops,
            "node_types": node_types,
        }
        return self._run_tool(
            "kg_search",
            {"query": query, "k": k, "node_types": node_types, "expand_hops": expand_hops},
            sql_text,
            params,
            result_key="results",
            retrieval_of=lambda rows, latency: {
                "k": k,
                "returned": len(rows),
                "rrf_top": rows[0]["rrf_score"] if rows else None,
                "hops": expand_hops,
                "latency_ms": latency,
            },
            requires_commit_check=True,
        )

    def kg_lexical_search(self, text: str, k: int = 10) -> dict[str, Any]:
        """mirrors fde_mcp.server:kg_lexical_search / kg.lexical_search."""
        sql_text = "SELECT node_key, node_type, label, sim FROM kg.lexical_search(%(eng)s::uuid, %(text)s, %(k)s)"
        params = {"eng": self.engagement_id, "text": text, "k": k}
        return self._run_tool(
            "kg_lexical_search",
            {"text": text, "k": k},
            sql_text,
            params,
            result_key="results",
            requires_commit_check=True,
        )

    def kg_traverse(
        self,
        start_keys: list[str],
        edge_types: list[str] | None = None,
        max_hops: int = 3,
        max_nodes: int = 500,
        direction: str = "out",
    ) -> dict[str, Any]:
        """mirrors fde_mcp.server:kg_traverse / kg.traverse. as-of-capable --
        pinned to this episode's commit's sealed_at (see module docstring)."""
        st = self._require_state()
        sql_text = """
            SELECT node_key, node_type, label, summary, depth, path, via_edge_key, via_edge_type, path_confidence
              FROM kg.traverse(%(eng)s::uuid, %(start_keys)s, %(edge_types)s::kg.edge_type[],
                                %(max_hops)s, %(max_nodes)s, %(direction)s, 0.0, %(as_of)s)
        """
        params = {
            "eng": self.engagement_id,
            "start_keys": start_keys,
            "edge_types": edge_types,
            "max_hops": max_hops,
            "max_nodes": max_nodes,
            "direction": direction,
            "as_of": st.pinned_sealed_at,
        }
        return self._run_tool(
            "kg_traverse",
            {
                "start_keys": start_keys,
                "edge_types": edge_types,
                "max_hops": max_hops,
                "max_nodes": max_nodes,
                "direction": direction,
            },
            sql_text,
            params,
            result_key="nodes",
            retrieval_of=lambda rows, latency: {
                "k": max_nodes,
                "returned": len(rows),
                "rrf_top": None,
                "hops": max_hops,
                "latency_ms": latency,
            },
        )

    def kg_dependency_closure(self, node_key: str, max_hops: int = 4) -> dict[str, Any]:
        """mirrors fde_mcp.server:kg_dependency_closure / kg.dependency_closure.
        Uses the 4-arg as-of overload, pinned to this episode's commit (see
        module docstring); the MCP tool keeps the live-reading 3-arg form."""
        st = self._require_state()
        sql_text = (
            "SELECT node_key, node_type, label, depth, path, path_confidence "
            "FROM kg.dependency_closure(%(eng)s::uuid, %(key)s, %(hops)s, %(as_of)s)"
        )
        params = {
            "eng": self.engagement_id,
            "key": node_key,
            "hops": max_hops,
            "as_of": st.pinned_sealed_at,
        }
        return self._run_tool(
            "kg_dependency_closure",
            {"node_key": node_key, "max_hops": max_hops},
            sql_text,
            params,
            result_key="closure",
        )

    def kg_impact_radius(self, node_key: str, max_hops: int = 4) -> dict[str, Any]:
        """mirrors fde_mcp.server:kg_impact_radius / kg.impact_radius. As-of
        pinned for the same reason as `kg_dependency_closure`."""
        st = self._require_state()
        sql_text = (
            "SELECT node_key, node_type, label, depth, path, path_confidence "
            "FROM kg.impact_radius(%(eng)s::uuid, %(key)s, %(hops)s, %(as_of)s)"
        )
        params = {
            "eng": self.engagement_id,
            "key": node_key,
            "hops": max_hops,
            "as_of": st.pinned_sealed_at,
        }
        return self._run_tool(
            "kg_impact_radius",
            {"node_key": node_key, "max_hops": max_hops},
            sql_text,
            params,
            result_key="impact",
        )

    def kg_get_node(self, node_key: str) -> dict[str, Any]:
        sql_text = "SELECT * FROM kg.node_current WHERE engagement_id = %(eng)s::uuid AND node_key = %(key)s"
        params = {"eng": self.engagement_id, "key": node_key}
        return self._run_tool(
            "kg_get_node",
            {"node_key": node_key},
            sql_text,
            params,
            result_key=None,
            single_row_key="node",
        )

    def kg_process_flow(self, process_key: str) -> dict[str, Any]:
        sql_text = "SELECT * FROM kg.process_flow(%(eng)s::uuid, %(key)s)"
        params = {"eng": self.engagement_id, "key": process_key}
        return self._run_tool(
            "kg_process_flow", {"process_key": process_key}, sql_text, params, result_key="steps"
        )

    def kg_propose(self, title: str, rationale: str, items: list[dict[str, Any]]) -> dict[str, Any]:
        """The terminal "propose a graph change" action. Deliberately does
        NOT write hitl.proposal (this role has no grant to -- see module
        docstring). Validates the same way
        `fde_training.rewards.r_schema_valid` would score it and records
        the (unwritten) proposal shape as a trace_step so
        `generate_traces.py`/reward functions can inspect it.
        """
        errors = []
        for item in items:
            op = item.get("op")
            if op not in (
                "add_node",
                "update_node",
                "retire_node",
                "add_edge",
                "update_edge",
                "retire_edge",
            ):
                errors.append(f"invalid op {op!r}")
                continue
            if not validate_tool_call_args("kg_propose", {"items": [item]}):
                errors.append(
                    f"item for subject_key={item.get('subject_key')!r} failed schema validation"
                )
        result = {
            "title": title,
            "rationale": rationale,
            "items": items,
            "valid": not errors,
            "errors": errors,
            "note": "SIMULATED -- rollout_env never writes hitl.proposal (read-only role).",
        }
        self._record_step(
            "kg_propose",
            {"title": title, "rationale": rationale, "items": items},
            result,
            None,
            0.0,
        )
        return result

    # -- plumbing -----------------------------------------------------------
    def _run_tool(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        sql_text: str,
        params: dict[str, Any],
        *,
        result_key: str | None,
        single_row_key: str | None = None,
        retrieval_of: Callable[[list[dict[str, Any]], float], dict[str, Any]] | None = None,
        requires_commit_check: bool = False,
    ) -> dict[str, Any]:
        st = self._require_state()
        if len(st.steps) >= self.tool_budget:
            msg = (
                f"episode {st.session_id} exceeded its tool-call budget ({self.tool_budget}); "
                f"reward functions should treat this as hop_budget_exceeded (see "
                f"trn.traversal_failure), not as a retry-worthy error."
            )
            raise ToolBudgetExceededError(msg)
        t0 = time.monotonic()
        with self._tool_transaction() as conn:
            if requires_commit_check:
                self._assert_commit_unchanged(conn)
            with conn.cursor() as cur:
                cur.execute(sql_text, params)
                rows = common.jsonify(cur.fetchall())
        latency_ms = (time.monotonic() - t0) * 1000.0

        result: dict[str, Any]
        if single_row_key is not None:
            row = rows[0] if rows else None
            result = {single_row_key: row}
        else:
            result = {"returned": len(rows), result_key: rows}

        retrieval = retrieval_of(rows, latency_ms) if retrieval_of else None
        self._record_step(tool_name, arguments, result, retrieval, latency_ms)
        return result

    def _record_step(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        result: Any,
        retrieval: dict[str, Any] | None,
        latency_ms: float,
    ) -> None:
        st = self._require_state()
        turn = len(st.steps) + 1
        st.steps.append(
            StepRecord(
                turn=turn,
                tool_name=tool_name,
                arguments=arguments,
                result=result,
                retrieval=retrieval,
                latency_ms=latency_ms,
            )
        )
        canonical_args = common.canonical_json(arguments)
        with self._tool_transaction() as conn, conn.cursor() as cur:
            # Two rows per real tool call, matching the OpenAI shape
            # export_sft.py/fde_training.rewards both expect: an assistant
            # turn carrying the tool_calls, then a tool-role response.
            cur.execute(
                """INSERT INTO trn.trace_step
                    (session_id, turn, role, tool_calls, trainable)
                   VALUES (%(sid)s, %(turn)s, 'assistant', %(tool_calls)s, true)""",
                {
                    "sid": st.session_id,
                    "turn": turn * 2 - 1,
                    "tool_calls": Jsonb(
                        [
                            {
                                "id": f"call_{turn}",
                                "type": "function",
                                "function": {"name": tool_name, "arguments": canonical_args},
                            }
                        ]
                    ),
                },
            )
            cur.execute(
                """INSERT INTO trn.trace_step
                    (session_id, turn, role, tool_call_id, tool_name, tool_result, trainable, retrieval, latency_ms)
                   VALUES (%(sid)s, %(turn)s, 'tool', %(call_id)s, %(tool_name)s, %(result)s, false, %(retrieval)s, %(latency_ms)s)""",
                {
                    "sid": st.session_id,
                    "turn": turn * 2,
                    "call_id": f"call_{turn}",
                    "tool_name": tool_name,
                    "result": Jsonb(common.truncate_tool_result(result)),
                    "retrieval": Jsonb(retrieval) if retrieval is not None else None,
                    "latency_ms": latency_ms,
                },
            )

    def step(self, tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """Generic dispatch, for callers (or a GRPO rollout loop) that treat
        the tool name as data rather than calling a bound method directly.
        """
        method = getattr(self, tool_name, None)
        if method is None or tool_name.startswith("_"):
            msg = f"unknown tool {tool_name!r}"
            raise ValueError(msg)
        return method(**arguments)  # type: ignore[no-any-return]

    def finish(self, final_answer: str, outcome: str = "pending") -> dict[str, Any]:
        st = self._require_state()
        latency_ms = int((time.monotonic() - st.started_monotonic) * 1000)
        total_tokens = (
            sum(len(json.dumps(s.result)) for s in st.steps) // 4 + len(final_answer) // 4
        )
        with self._tool_transaction() as conn, conn.cursor() as cur:
            cur.execute(
                """INSERT INTO trn.trace_step (session_id, turn, role, content, trainable)
                   VALUES (%(sid)s, %(turn)s, 'assistant', %(content)s, true)""",
                {"sid": st.session_id, "turn": len(st.steps) * 2 + 1, "content": final_answer},
            )
            cur.execute(
                # `outcome` is deliberately NOT written. It stays at the
                # 'pending' default so this episode can never match
                # trn.sft_export's `outcome IN ('accepted','corrected')`
                # filter -- rollout exploration is excluded from the
                # supervised set by construction rather than by a filter
                # someone has to remember. The episode's terminal state goes
                # in final_output, which the RL harness reads and the SFT
                # export ignores. Enforced by grant in db/012.
                """UPDATE trn.trace_session
                      SET ended_at = now(),
                          final_output = %(final_output)s,
                          total_tokens = %(tokens)s, latency_ms = %(latency)s
                    WHERE session_id = %(sid)s""",
                {
                    "sid": st.session_id,
                    "final_output": Jsonb({"episode_outcome": outcome}),
                    "tokens": total_tokens,
                    "latency": latency_ms,
                },
            )
        st.finished = True
        log.info(
            "episode_finished",
            session_id=st.session_id,
            outcome=outcome,
            tool_calls=len(st.steps),
            latency_ms=latency_ms,
        )
        return {
            "session_id": st.session_id,
            "tool_call_count": len(st.steps),
            "latency_ms": latency_ms,
        }

    def transcript(self) -> list[dict[str, Any]]:
        """The episode so far, in the exact message shape
        `fde_training.rewards.EpisodeLog.from_completion` consumes -- lets a
        training loop score partial/completed episodes with the same reward
        functions used everywhere else in this pipeline."""
        st = self._require_state()
        messages: list[dict[str, Any]] = []
        for s in st.steps:
            messages.append(
                {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": f"call_{s.turn}",
                            "type": "function",
                            "function": {
                                "name": s.tool_name,
                                "arguments": common.canonical_json(s.arguments),
                            },
                        }
                    ],
                }
            )
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": f"call_{s.turn}",
                    "name": s.tool_name,
                    "content": common.canonical_json(s.result),
                }
            )
        return messages


class _RoleScopedTransaction:
    """`with pool.connection() as conn: SET LOCAL ROLE ...; yield conn` as a
    reusable context manager, mirroring `fde_mcp.db.tool_transaction`'s
    contract but for the sync `psycopg_pool.ConnectionPool`."""

    def __init__(self, pool: ConnectionPool, role: str) -> None:
        self.pool = pool
        self.role = role
        self._cm: Any = None
        self._conn: psycopg.Connection[Any] | None = None

    def __enter__(self) -> psycopg.Connection[Any]:
        self._cm = self.pool.connection()
        self._conn = self._cm.__enter__()
        self._conn.execute(sql.SQL("SET LOCAL ROLE {}").format(sql.Identifier(self.role)))
        return self._conn

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> Any:
        return self._cm.__exit__(exc_type, exc, tb)


# ===========================================================================
# TRL `tools=[...]` adapter -- typed Python functions with Google-style
# docstrings bound to one RolloutEnv instance, per the VERIFIED GROUNDING
# fact that GRPOTrainer accepts either `tools=[...]` or `environment_factory`.
# ===========================================================================
def as_tool_functions(env: RolloutEnv) -> list[Callable[..., Any]]:
    def kg_search(query: str, k: int = 20, expand_hops: int = 2) -> dict[str, Any]:
        """Hybrid ANN + graph retrieval over the knowledge graph.

        Args:
            query: Natural-language question to retrieve evidence for.
            k: Maximum number of results to return.
            expand_hops: Graph-expansion depth from the top ANN seeds.

        Returns:
            A dict with `results`: a list of {node_key, node_type, label,
            summary, rrf_score, provenance} ranked by relevance.
        """
        return env.kg_search(query=query, k=k, expand_hops=expand_hops)

    def kg_traverse(
        start_keys: list[str], max_hops: int = 3, direction: str = "out"
    ) -> dict[str, Any]:
        """Bounded multi-hop traversal from one or more start nodes.

        Args:
            start_keys: node_key values to start from.
            max_hops: Depth ceiling (<=6).
            direction: 'out', 'in', or 'both'.

        Returns:
            A dict with `nodes`: reachable nodes with depth/path/confidence.
        """
        return env.kg_traverse(start_keys=start_keys, max_hops=max_hops, direction=direction)

    def kg_get_node(node_key: str) -> dict[str, Any]:
        """Fetch one live node.

        Args:
            node_key: The node's stable key.

        Returns:
            A dict with `node`: the node row, or {"node": None} if absent.
        """
        return env.kg_get_node(node_key=node_key)

    def kg_propose(title: str, rationale: str, items: list[dict[str, Any]]) -> dict[str, Any]:
        """Propose a graph change (terminal action; not actually written).

        Args:
            title: Short proposal title.
            rationale: Why this change is warranted.
            items: List of proposal items, each shaped like
                hitl.proposal_item (op/node_type|edge_type/subject_key/
                payload/source_ids/agent_confidence).

        Returns:
            A dict with `valid`, `errors`, and the echoed proposal.
        """
        return env.kg_propose(title=title, rationale=rationale, items=items)

    return [kg_search, kg_traverse, kg_get_node, kg_propose]


# ===========================================================================
# verl BaseTool adapter (best-effort sketch; verl is not installed in this
# environment and this has NOT been exercised against a real verl runtime --
# flagged honestly rather than silently assumed correct). Written against
# verl's documented multi-turn tool convention: an async `execute(instance_id,
# parameters, **kwargs) -> (tool_response_str, reward, metrics_dict)`.
# ===========================================================================
VerlRolloutTool: type[Any] | None
try:
    from verl.tools.base_tool import BaseTool  # type: ignore[import-not-found]

    class VerlRolloutTool(BaseTool):  # type: ignore[no-redef,misc]
        def __init__(self, config: dict[str, Any], tool_schema: Any) -> None:
            super().__init__(config, tool_schema)
            self._envs: dict[str, RolloutEnv] = {}

        async def create(self, instance_id: str, **kwargs: Any) -> str:
            engagement_id = kwargs["engagement_id"]
            env = RolloutEnv(engagement_id=engagement_id)
            env.reset(
                task_kind=kwargs.get("task_kind", "rl_rollout"),
                task_input=kwargs.get("task_input", {}),
            )
            self._envs[instance_id] = env
            return instance_id

        async def execute(
            self, instance_id: str, parameters: dict[str, Any], **kwargs: Any
        ) -> tuple[str, float, dict[str, Any]]:
            import asyncio  # noqa: PLC0415

            env = self._envs[instance_id]
            tool_name = parameters.pop("tool_name")
            try:
                result = await asyncio.to_thread(env.step, tool_name, parameters)
                return json.dumps(result), 0.0, {}
            except (ToolBudgetExceededError, StaleCommitError) as exc:
                return json.dumps({"error": str(exc)}), -1.0, {"terminal": True}

        async def release(self, instance_id: str, **kwargs: Any) -> None:
            self._envs.pop(instance_id, None)

except ImportError:  # pragma: no cover -- verl not installed in this environment
    VerlRolloutTool = None
