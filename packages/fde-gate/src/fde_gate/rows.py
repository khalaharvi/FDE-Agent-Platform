"""rows.py -- turning psycopg rows into things that can be sent over HTTP.

`dict_row` hands back native `uuid.UUID`, `datetime`, `Decimal` and
`memoryview` objects for the corresponding column types. Every one of them
raises `TypeError` inside `json.dumps`, and every response this service
produces is either JSON or a Jinja2 render of the same dicts.

Deliberately not imported from `fde_mcp.tools._base`
----------------------------------------------------
That module has an identical `jsonify`, but it is private to the MCP tool
boundary and its coercion is tuned for what an LLM reads (it is called twice
on purpose, once for the result and once before trace emission). Importing
across the boundary would make a change made for the model's benefit a
silent change to what a reviewer's browser receives. Twenty lines is the
cheaper coupling.
"""

from __future__ import annotations

import uuid as uuid_mod
from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import psycopg

__all__ = ["fetchall", "fetchone", "jsonify"]


def jsonify(value: Any) -> Any:
    """Recursively coerce psycopg's native types into JSON-safe values."""
    if isinstance(value, dict):
        return {k: jsonify(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [jsonify(v) for v in value]
    return _scalar(value)


def _scalar(value: Any) -> Any:
    """The leaf cases. Split out from `jsonify` so the recursion (two lines)
    reads separately from the type table (four), rather than as one function
    where the interesting part is buried among the boring part.
    """
    if isinstance(value, uuid_mod.UUID):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, memoryview | bytes):
        return bytes(value).decode("utf-8", errors="replace")
    return value


async def fetchall(cur: psycopg.AsyncCursor[dict[str, Any]]) -> list[dict[str, Any]]:
    """Every row, JSON-safe."""
    rows = await cur.fetchall()
    return [jsonify(row) for row in rows]


async def fetchone(cur: psycopg.AsyncCursor[dict[str, Any]]) -> dict[str, Any] | None:
    """The next row, JSON-safe, or None."""
    row = await cur.fetchone()
    return None if row is None else dict(jsonify(row))
