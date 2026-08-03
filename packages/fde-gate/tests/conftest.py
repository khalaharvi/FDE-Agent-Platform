"""Shared fixtures for the fde_gate test suite.

Design
------
Same two-layer arrangement as `fde_mcp`'s conftest, for the same reasons:

* Tests that need Postgres are marked `requires_db` (file-wide via
  `pytestmark`), and `pytest_collection_modifyitems` turns that into a clean
  SKIP when `FDE_DB_DSN` is unset -- so `pytest packages/fde-gate` works on a
  machine with no database instead of reporting a screenful of connection
  errors that are really one missing variable.
* The async pool is a process-wide singleton bound to whichever event loop
  first opened it, and `asyncio_mode = "auto"` gives each test its own loop.
  `_reset_db_pool` closes it after every test so the next one opens a fresh
  pool on ITS loop.

Fixtures are seeded with a plain synchronous `psycopg.connect` as the calling
OS role (owner-equivalent), deliberately bypassing `SET LOCAL ROLE` -- the
seed stands in for work done by other components (an agent proposing, an FDE
registering evidence), and making it go through the gate service's own roles
would be testing the fixture rather than the code.

Why the proposal and workflow fixtures are FACTORIES, not shared rows
----------------------------------------------------------------------
Nearly every test here mutates what it looks at: approving changes a status,
merging seals a commit, failing a step burns an attempt. A session-scoped
"the proposal" would make each test's outcome depend on which tests ran
before it, and the failure mode is the worst kind -- green in isolation, red
under `-k`, or vice versa. So the engagement, its commit, its sources, its
graph nodes and its reviewer roster are session-scoped (nothing mutates
them), and every proposal, workflow and run is built fresh by a factory.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator, Iterator
from typing import Any

import psycopg
import pytest
import pytest_asyncio
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

os.environ.setdefault("FDE_AGENT_NAME", "engagement")
os.environ.setdefault("FDE_AGENT_RUNTIME_ARN", "pytest:fde-gate-tests")

from gate_seed import AGENT_PRINCIPAL, BOUND_NODE_KEYS, COMPLIANCE, OWNER, SME, STRANGER

from fde_gate.config import get_gate_settings
from fde_mcp import db
from fde_mcp.config import get_settings


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Skip every `requires_db` test cleanly when no DSN is configured."""
    if os.environ.get("FDE_DB_DSN"):
        return
    skip_db = pytest.mark.skip(reason="FDE_DB_DSN not set; no live Postgres to test against")
    for item in items:
        if "requires_db" in item.keywords:
            item.add_marker(skip_db)


@pytest.fixture(autouse=True)
def _fresh_settings() -> Iterator[None]:
    """Re-read configuration for every test.

    Both caches, because `fde_gate.config.Settings.db` is sourced from
    `fde_mcp.config` -- clearing only one leaves a test that monkeypatched
    `FDE_DB_*` looking at the previous test's database.
    """
    get_gate_settings.cache_clear()
    get_settings.cache_clear()
    yield
    get_gate_settings.cache_clear()
    get_settings.cache_clear()


@pytest_asyncio.fixture(autouse=True)
async def _reset_db_pool() -> AsyncIterator[None]:
    """Close the async pool after each test; see the module docstring."""
    yield
    await db.close_pool()


@pytest.fixture(scope="session")
def engagement_id() -> str:
    return str(uuid.uuid4())


def _connect() -> psycopg.Connection[dict[str, Any]]:
    return psycopg.connect(os.environ["FDE_DB_DSN"], autocommit=True, row_factory=dict_row)


@pytest.fixture(scope="session")
def seed(engagement_id: str) -> dict[str, Any]:
    """One engagement with a sealed commit, a source, three graph nodes, and
    a reviewer roster. Nothing in the suite mutates any of it.
    """
    with _connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            -- content_digest is set here because `wf.publish_workflow` copies
            -- it onto the workflow as `pinned_digest`, which is what makes a
            -- tampered replay of the pinned commit detectable. A real commit
            -- gets it from `hitl.merge_proposal`; a hand-seeded one has to
            -- carry it too or the publish path looks like it silently skips
            -- the pin.
            INSERT INTO kg.commit (engagement_id, status, title, authored_by,
                                   sealed_by, sealed_at, content_digest)
            VALUES (%(eng)s, 'sealed', 'genesis (pytest fixture)', 'pytest', 'pytest', now(),
                    encode(digest('genesis (pytest fixture)', 'sha256'), 'hex'))
            RETURNING commit_id, commit_uuid, sealed_at, content_digest
            """,
            {"eng": engagement_id},
        )
        commit = cur.fetchone()
        assert commit is not None
        commit_id = commit["commit_id"]

        cur.execute(
            """
            INSERT INTO kg.source (engagement_id, source_kind, title, uri, captured_at,
                                   captured_by)
            VALUES (%(eng)s, 'interview', 'RevOps interview (pytest fixture)',
                    's3://fixtures/interview.txt', now(), 'pytest')
            RETURNING source_id
            """,
            {"eng": engagement_id},
        )
        source_row = cur.fetchone()
        assert source_row is not None
        source_id = source_row["source_id"]

        node_ids: dict[str, int] = {}
        for node_key in BOUND_NODE_KEYS:
            cur.execute(
                """
                INSERT INTO kg.node (engagement_id, node_key, node_type, label, summary,
                                     commit_id)
                VALUES (%(eng)s, %(key)s, 'activity', %(label)s, 'pytest fixture node',
                        %(cid)s)
                RETURNING node_id
                """,
                {
                    "eng": engagement_id,
                    "key": node_key,
                    "label": node_key.replace(".", " "),
                    "cid": commit_id,
                },
            )
            row = cur.fetchone()
            assert row is not None
            node_ids[node_key] = row["node_id"]

        reviewer_ids: dict[str, int] = {}
        roster = (
            (SME, "RevOps SME", ("ontology", "factual")),
            (COMPLIANCE, "Compliance", ("ontology", "control")),
            (OWNER, "Process Owner", ("factual", "automation", "ontology")),
        )
        for principal, display_name, gate_kinds in roster:
            # ON CONFLICT because `hitl.reviewer.principal` is globally
            # unique, not per-engagement: a person is a person across every
            # engagement. The session's engagement_id is fresh, so the
            # AUTHORITY rows below never collide -- only the roster does, and
            # only when a previous run of this suite left it behind.
            cur.execute(
                """
                INSERT INTO hitl.reviewer (principal, display_name)
                VALUES (%(p)s, %(d)s)
                ON CONFLICT (principal) DO UPDATE SET display_name = EXCLUDED.display_name
                RETURNING reviewer_id
                """,
                {"p": principal, "d": display_name},
            )
            row = cur.fetchone()
            assert row is not None
            reviewer_ids[principal] = row["reviewer_id"]
            for gate_kind in gate_kinds:
                cur.execute(
                    """
                    INSERT INTO hitl.reviewer_authority
                        (reviewer_id, engagement_id, gate_kind, granted_by)
                    VALUES (%(r)s, %(eng)s, %(k)s, 'pytest')
                    """,
                    {"r": row["reviewer_id"], "eng": engagement_id, "k": gate_kind},
                )

        # A reviewer who exists but holds no authority on this engagement.
        # `record_decision` must refuse them by authority, not by identity --
        # a different failure with a different message.
        cur.execute(
            """
            INSERT INTO hitl.reviewer (principal, display_name)
            VALUES (%(p)s, 'Unauthorised Stranger')
            ON CONFLICT (principal) DO UPDATE SET display_name = EXCLUDED.display_name
            RETURNING reviewer_id
            """,
            {"p": STRANGER},
        )
        stranger = cur.fetchone()
        assert stranger is not None
        reviewer_ids[STRANGER] = stranger["reviewer_id"]

    return {
        "engagement_id": engagement_id,
        "commit_id": commit_id,
        "source_id": source_id,
        "node_ids": node_ids,
        "reviewer_ids": reviewer_ids,
    }


@pytest.fixture
def make_proposal(seed: dict[str, Any]) -> Any:
    """Build a proposal with items, optionally submitted.

    `submit=True` runs `hitl.submit_proposal`, which is what materialises the
    gate set -- an unsubmitted proposal has no gates at all, so most tests
    want it.
    """

    def _make(
        *,
        title: str = "pytest proposal",
        items: list[dict[str, Any]] | None = None,
        submit: bool = True,
        trace_session_id: str | None = None,
        authored_by: str = AGENT_PRINCIPAL,
        expires_at: str | None = None,
    ) -> dict[str, Any]:
        item_specs = items or [
            {
                "op": "add_node",
                "node_type": "activity",
                "subject_key": f"act.pytest_{uuid.uuid4().hex[:8]}",
                "payload": {"label": "Pytest Activity", "summary": "Seeded by a test."},
                "agent_confidence": 0.8,
            }
        ]
        with _connect() as conn, conn.cursor() as cur:
            if trace_session_id is not None:
                cur.execute(
                    """
                    INSERT INTO trn.trace_session (session_id, engagement_id, agent_name,
                                                   agent_runtime_arn, model_id,
                                                   base_commit_id, task_kind, task_input)
                    VALUES (%(sid)s, %(eng)s, 'engagement', 'pytest:runtime',
                            'pytest-model', %(cid)s, 'map_workflow', '{}'::jsonb)
                    """,
                    {
                        "sid": trace_session_id,
                        "eng": seed["engagement_id"],
                        "cid": seed["commit_id"],
                    },
                )

            cur.execute(
                """
                INSERT INTO hitl.proposal (engagement_id, title, rationale, authored_by,
                                           agent_name, base_commit_id, trace_session_id)
                VALUES (%(eng)s, %(title)s, 'Seeded by a pytest fixture.', %(author)s,
                        'engagement', %(cid)s, %(sid)s)
                RETURNING proposal_id
                """,
                {
                    "eng": seed["engagement_id"],
                    "title": title,
                    "author": authored_by,
                    "cid": seed["commit_id"],
                    "sid": trace_session_id,
                },
            )
            row = cur.fetchone()
            assert row is not None
            proposal_id = row["proposal_id"]

            item_ids: list[int] = []
            for ordinal, spec in enumerate(item_specs, start=1):
                cur.execute(
                    """
                    INSERT INTO hitl.proposal_item
                        (proposal_id, ordinal, op, node_type, edge_type, subject_key,
                         payload, source_ids, agent_confidence)
                    VALUES (%(pid)s, %(ord)s, %(op)s, %(ntype)s, %(etype)s, %(key)s,
                            %(payload)s, %(sources)s, %(conf)s)
                    RETURNING item_id
                    """,
                    {
                        "pid": proposal_id,
                        "ord": ordinal,
                        "op": spec["op"],
                        "ntype": spec.get("node_type"),
                        "etype": spec.get("edge_type"),
                        "key": spec["subject_key"],
                        "payload": Jsonb(spec["payload"]),
                        "sources": [seed["source_id"]],
                        "conf": spec.get("agent_confidence", 0.8),
                    },
                )
                item_row = cur.fetchone()
                assert item_row is not None
                item_ids.append(item_row["item_id"])

            if submit:
                cur.execute(
                    "SELECT proposal_id FROM hitl.submit_proposal(%(pid)s)",
                    {"pid": proposal_id},
                )
            if expires_at is not None:
                cur.execute(
                    "UPDATE hitl.proposal SET expires_at = %(exp)s::timestamptz "
                    "WHERE proposal_id = %(pid)s",
                    {"pid": proposal_id, "exp": expires_at},
                )

            cur.execute(
                """
                SELECT gate_id, gate_kind::text AS gate_kind, quorum
                  FROM hitl.proposal_gate WHERE proposal_id = %(pid)s ORDER BY gate_id
                """,
                {"pid": proposal_id},
            )
            gates = list(cur.fetchall())

        return {
            "proposal_id": proposal_id,
            "item_ids": item_ids,
            "gates": gates,
            "gate_ids": [g["gate_id"] for g in gates],
            "trace_session_id": trace_session_id,
        }

    return _make


@pytest.fixture
def make_workflow(seed: dict[str, Any]) -> Any:
    """Build a workflow with steps and (by default) faithfulness bindings.

    `bind=False` leaves the steps unbound, which is what a test of the
    publish gate needs -- `wf.assert_faithful` must refuse it.
    """

    def _make(
        steps: list[dict[str, Any]],
        *,
        slug: str | None = None,
        publish: bool = True,
        bind: bool = True,
        runnable_by: list[str] | None = None,
    ) -> int:
        with _connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO wf.workflow (engagement_id, slug, title, pinned_commit_id,
                                         root_process_key, authored_by, runnable_by)
                VALUES (%(eng)s, %(slug)s, 'Pytest Workflow', %(cid)s,
                        'proc.quote_to_cash', 'agent:workflow', %(runnable)s)
                RETURNING workflow_id
                """,
                {
                    "eng": seed["engagement_id"],
                    "slug": slug or f"pytest-{uuid.uuid4().hex[:8]}",
                    "cid": seed["commit_id"],
                    "runnable": runnable_by or [],
                },
            )
            row = cur.fetchone()
            assert row is not None
            workflow_id = row["workflow_id"]

            for ordinal, spec in enumerate(steps, start=1):
                cur.execute(
                    """
                    INSERT INTO wf.step (workflow_id, step_key, ordinal, kind, title,
                                         instruction, tool_name, tool_args, human_prompt,
                                         human_schema, branches, sor_adapter_key,
                                         sor_write_op, requires_human, timeout_seconds,
                                         on_failure)
                    VALUES (%(wid)s, %(key)s, %(ord)s, %(kind)s::wf.step_kind, %(title)s,
                            %(instr)s, %(tool)s, %(args)s, %(prompt)s, %(schema)s,
                            %(branches)s, %(adapter)s, %(op)s, %(human)s, %(timeout)s,
                            %(onfail)s)
                    RETURNING step_id
                    """,
                    {
                        "wid": workflow_id,
                        "key": spec["step_key"],
                        "ord": ordinal,
                        "kind": spec["kind"],
                        "title": spec.get("title", spec["step_key"]),
                        "instr": spec.get("instruction", "Do the pytest thing."),
                        "tool": spec.get("tool_name"),
                        "args": Jsonb(spec["tool_args"]) if "tool_args" in spec else None,
                        "prompt": spec.get("human_prompt"),
                        "schema": Jsonb(spec["human_schema"]) if "human_schema" in spec else None,
                        "branches": Jsonb(spec["branches"]) if "branches" in spec else None,
                        "adapter": spec.get("sor_adapter_key"),
                        "op": spec.get("sor_write_op"),
                        "human": spec.get("requires_human", False),
                        "timeout": spec.get("timeout_seconds", 900),
                        "onfail": spec.get("on_failure", "halt"),
                    },
                )
                step_row = cur.fetchone()
                assert step_row is not None
                if bind and spec["kind"] != "notify":
                    # `bindings` is (subject_key, relation) pairs; one
                    # `implements` binding to the first seeded node is what a
                    # step needs to be publishable, and is what most tests
                    # want. A test about how bindings are ORDERED needs a step
                    # carrying more than one, hence the override.
                    pairs = spec.get("bindings", ((BOUND_NODE_KEYS[0], "implements"),))
                    for subject_key, relation in pairs:
                        cur.execute(
                            """
                            INSERT INTO wf.step_binding (step_id, subject_kind, subject_key,
                                                         relation, pinned_label)
                            VALUES (%(sid)s, 'node', %(key)s, %(rel)s, 'pytest fixture')
                            """,
                            {"sid": step_row["step_id"], "key": subject_key, "rel": relation},
                        )

            if publish:
                cur.execute(
                    "SELECT workflow_id FROM wf.publish_workflow(%(wid)s, %(by)s)",
                    {"wid": workflow_id, "by": OWNER},
                )
        return int(workflow_id)

    return _make


@pytest.fixture
def sql() -> Any:
    """Run a query as the owner, for assertions the service roles cannot make.

    Several tests need to read `trn.trace_session.outcome` (only
    `fde_gate_service` may write it, and only `fde_training` and the owner
    read it comfortably) or to age a row's timestamp to trigger a sweep.
    Doing that through the service roles would mean widening their grants for
    the test suite's convenience, which is the opposite of the point.
    """

    def _sql(query: str, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        with _connect() as conn, conn.cursor() as cur:
            cur.execute(query, params or {})
            if cur.description is None:
                return []
            return list(cur.fetchall())

    return _sql
