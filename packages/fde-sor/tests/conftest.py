"""Shared fixtures for the fde_sor test suite.

Mirrors `packages/fde-mcp/tests/conftest.py`: `requires_db` tests skip cleanly
when `FDE_DB_DSN` is unset, the async pool is closed after every test so the
next one opens its own on its own event loop, and the fixture graph is seeded
with a plain synchronous `psycopg.connect` as the calling OS role (which
db/010 gives owner-equivalent rights) rather than through `SET LOCAL ROLE`.

What this file seeds beyond the graph
--------------------------------------
The drift end-to-end test needs a `gated_by` edge -- `detect_control_bypass`
joins `kg.edge_current` where `edge_type = 'gated_by'` and finds nothing
without one -- plus at least `p_min_samples` (10) distinct cases, since the
detector's HAVING clause discards a smaller sample as noise. Both are in the
JSONL export each drift test generates rather than checks in: the timestamps
have to fall inside the detector's 90-day lookback window, and a checked-in
file with fixed dates would start silently passing-by-finding-nothing the
moment it aged out.

`FDE_ACTOR_HASH_SALT` is set here so the whole suite runs with a real,
deterministic salt. Without it, every mapping that declares an `actor_field`
would fail closed against Secrets Manager -- which is the correct production
behaviour and is tested explicitly in `test_hashing.py`.

Why the record builders are fixtures rather than an importable helper module
----------------------------------------------------------------------------
Test modules here reach shared data ONLY through fixture injection. The
obvious alternative -- a `sor_fixtures.py` next door and `from sor_fixtures
import ...` -- resolves through `sys.path`, which works under pytest's default
`prepend` import mode and breaks under `--import-mode=importlib`. Fixtures
work under both, and this package should not be the reason a workspace-wide
pytest setting cannot be changed.
"""

from __future__ import annotations

import json
import os
import uuid
from collections.abc import AsyncIterator, Callable, Iterator
from pathlib import Path
from typing import Any

import psycopg
import pytest
import pytest_asyncio
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

os.environ.setdefault("FDE_AGENT_NAME", "engagement")
os.environ.setdefault("FDE_AGENT_RUNTIME_ARN", "pytest:fde-sor-tests")
os.environ.setdefault("FDE_ACTOR_HASH_SALT", "pytest-deterministic-salt")

from fde_mcp import db
from fde_sor import hashing
from fde_sor.config import get_settings

# docs/08 section 2.1's Jira example, plus the fields the shared pipeline maps
# beyond the four required ones.
JIRA_MAPPING: dict[str, Any] = {
    "case_id_field": "key",
    "activity_field": "fields.status.name",
    "activity_map": {
        "In Review": "act.legal_review",
        "Deal Desk Approved": "act.discount_approval",
        "Threshold Checked": "ctrl.discount_threshold",
    },
    "actor_field": "fields.assignee.accountId",
    "actor_role_map": {"acct-deal-desk": "role.deal_desk"},
    "timestamp_field": "fields.updated",
    "system_object_field": "fields.project.key",
    "attributes_fields": ["fields.priority"],
}


def _jira_record(
    case_ref: str, status: str, updated: str, *, actor: str = "acct-deal-desk"
) -> dict[str, Any]:
    """One raw Jira-shaped record, matching `JIRA_MAPPING`."""
    return {
        "key": case_ref,
        "fields": {
            "status": {"name": status},
            "assignee": {"accountId": actor},
            "updated": updated,
            "project": {"key": "CPQ"},
            "priority": "High",
        },
    }


def _write_jsonl(path: Path, records: list[dict[str, Any]]) -> str:
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")
    return str(path)


@pytest.fixture(scope="session")
def jira_mapping() -> dict[str, Any]:
    return JIRA_MAPPING


@pytest.fixture(scope="session")
def make_record() -> Callable[..., dict[str, Any]]:
    return _jira_record


@pytest.fixture(scope="session")
def write_jsonl() -> Callable[[Path, list[dict[str, Any]]], str]:
    return _write_jsonl


def pytest_configure(config: pytest.Config) -> None:
    """Register `requires_cdc` locally.

    The root `pyproject.toml` owns the marker list and runs pytest with
    `--strict-markers`; registering here keeps `pytest packages/fde-sor`
    working before the integrator adds `requires_cdc` to that list, and is a
    no-op afterwards.
    """
    config.addinivalue_line(
        "markers",
        "requires_cdc: needs a Postgres with wal_level=logical and the wal2json "
        "output plugin (skipped where logical decoding is unavailable)",
    )


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    if os.environ.get("FDE_DB_DSN"):
        return
    skip_db = pytest.mark.skip(reason="FDE_DB_DSN not set; no live Postgres to test against")
    for item in items:
        if "requires_db" in item.keywords or "requires_cdc" in item.keywords:
            item.add_marker(skip_db)


@pytest_asyncio.fixture(autouse=True)
async def _reset_db_pool() -> AsyncIterator[None]:
    yield
    await db.close_pool()


@pytest.fixture(autouse=True)
def _reset_sor_caches() -> Iterator[None]:
    """Settings and the salt cache are process-wide; a test that monkeypatches
    the environment must not leak into the next one.
    """
    get_settings.cache_clear()
    hashing.clear_salt_cache()
    yield
    get_settings.cache_clear()
    hashing.clear_salt_cache()


def dsn() -> str:
    return os.environ["FDE_DB_DSN"]


NODES: list[tuple[str, str, str, dict[str, Any]]] = [
    ("proc.quote_to_cash", "process", "Quote to Cash", {}),
    ("act.legal_review", "activity", "Legal Review", {}),
    ("act.discount_approval", "activity", "Discount Approval", {}),
    ("ctrl.discount_threshold", "control", "Discount Threshold Control", {}),
    ("role.deal_desk", "role", "Deal Desk Analyst", {"is_role_title": True}),
    ("sys.jira", "system", "Jira", {}),
]

# The edge detect_control_bypass keys off: an activity gated by a control.
EDGES: list[tuple[str, str, str]] = [
    ("act.discount_approval", "gated_by", "ctrl.discount_threshold"),
    ("act.legal_review", "precedes", "act.discount_approval"),
    ("role.deal_desk", "performs", "act.discount_approval"),
]


@pytest.fixture(scope="session")
def engagement_id() -> str:
    return str(uuid.uuid4())


@pytest.fixture(scope="session")
def seed(engagement_id: str) -> dict[str, Any]:
    """A graph with a gated activity, and one registered rest_poll adapter."""
    with (
        psycopg.connect(dsn(), autocommit=True, row_factory=dict_row) as conn,
        conn.cursor() as cur,
    ):
        cur.execute(
            """
            INSERT INTO kg.commit (engagement_id, status, title, authored_by, sealed_by, sealed_at)
            VALUES (%(eng)s, 'sealed', 'genesis (fde-sor fixture)', 'pytest', 'pytest', now())
            RETURNING commit_id
            """,
            {"eng": engagement_id},
        )
        commit = cur.fetchone()
        assert commit is not None
        commit_id = commit["commit_id"]

        for node_key, node_type, label, attributes in NODES:
            cur.execute(
                "INSERT INTO kg.node "
                "(engagement_id, node_key, node_type, label, attributes, commit_id) "
                "VALUES (%(eng)s, %(key)s, %(ntype)s, %(label)s, %(attrs)s, %(cid)s)",
                {
                    "eng": engagement_id,
                    "key": node_key,
                    "ntype": node_type,
                    "label": label,
                    "attrs": Jsonb(attributes),
                    "cid": commit_id,
                },
            )
        for src, edge_type, dst in EDGES:
            cur.execute(
                """
                INSERT INTO kg.edge
                    (engagement_id, edge_key, src_key, dst_key, edge_type, commit_id, human_confirmed)
                VALUES (%(eng)s, %(ekey)s, %(src)s, %(dst)s, %(etype)s, %(cid)s, true)
                """,
                {
                    "eng": engagement_id,
                    "ekey": f"{src}|{edge_type}|{dst}",
                    "src": src,
                    "dst": dst,
                    "etype": edge_type,
                    "cid": commit_id,
                },
            )

        cur.execute(
            """
            INSERT INTO sor.adapter
                (engagement_id, adapter_key, system_node_key, kind, mapping, poll_cron)
            VALUES (%(eng)s, 'jira-prod', 'sys.jira', 'rest_poll', %(mapping)s, '*/15 * * * *')
            RETURNING adapter_id
            """,
            {"eng": engagement_id, "mapping": Jsonb(JIRA_MAPPING)},
        )
        adapter = cur.fetchone()
        assert adapter is not None

    return {
        "engagement_id": engagement_id,
        "commit_id": commit_id,
        "adapter_id": adapter["adapter_id"],
        "adapter_key": "jira-prod",
    }
