"""fde_mcp -- the FDE Platform MCP server.

A thin, typed MCP tool surface over the knowledge-graph SQL functions in
db/008_retrieval.sql, db/004_hitl_gates.sql, db/007_drift.sql, and
db/006_workflows.sql. See `fde_mcp.server` for the module docstring
covering deployment shapes, the security model, and the error-handling
contract every tool honours.

What's re-exported here is deliberately small: `get_settings`/`get_logger`
because every other package in the workspace (fde-agents included) needs a
stable, typed way to read this package's configuration and logging without
reaching into `fde_mcp.config`/`fde_mcp.logging` module internals, and
`build_server`/`main` because they are the two ways anything outside this
package starts the server. Everything else (the tool implementations, the
DB pool, the embedding client) is an implementation detail reached through
its own submodule, not through the package root.
"""

from __future__ import annotations

from importlib import metadata

from fde_mcp.config import Settings, get_settings
from fde_mcp.logging import get_logger
from fde_mcp.server import build_server, main

try:
    __version__ = metadata.version("fde-mcp")
except metadata.PackageNotFoundError:  # pragma: no cover - editable/unbuilt checkout
    __version__ = "0.0.0.dev0"

__all__ = [
    "Settings",
    "__version__",
    "build_server",
    "get_logger",
    "get_settings",
    "main",
]
