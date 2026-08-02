"""`python -m fde_mcp` entrypoint. Also the target of the `fde-mcp` console
script (see pyproject.toml) -- both invocation paths end up calling the
exact same `fde_mcp.server.main`, so there is exactly one place that decides
transport/host/port for a locally-run server versus one installed as a
package.

The server is configured entirely by environment variables, not flags --
`--help` exists so the reflexive first command a developer types prints the
contract instead of starting a stdio server that appears to hang (or, on
EOF, dies with a traceback that looks like a crash).
"""

from __future__ import annotations

import sys

_USAGE = """\
usage: fde-mcp

The FDE Platform MCP server (21 tools over the knowledge graph).
Configured via environment variables, not flags:

  FDE_MCP_TRANSPORT   "http" (default; serves on FDE_MCP_HOST/FDE_MCP_PORT)
                      or "stdio" (for a spawning MCP client / local dev)
  FDE_DB_DSN          Postgres DSN with the db/ migrations applied
                      (or FDE_DB_SECRET_ARN / FDE_DB_IAM_AUTH=1 -- see
                      packages/fde-mcp/src/fde_mcp/config.py for every
                      variable and its default)

Quick local run:
  FDE_MCP_TRANSPORT=stdio FDE_DB_DSN=postgresql:///fde uv run fde-mcp

Docs: packages/fde-mcp/README.md and docs/05-mcp-surface.md
"""


def main() -> None:
    args = sys.argv[1:]
    if args and args[0] in ("-h", "--help"):
        print(_USAGE, end="")
        raise SystemExit(0)
    if args:
        print(
            f"fde-mcp: unknown argument {args[0]!r} -- the server takes no flags;\n"
            "it is configured via FDE_* environment variables. Run `fde-mcp --help`.",
            file=sys.stderr,
        )
        raise SystemExit(2)

    # Imported after argv handling on purpose: fde_mcp.server pulls in FastMCP
    # and registers all 21 tools at import time, and `--help` should answer
    # instantly with zero side effects.
    from fde_mcp.server import main as server_main  # noqa: PLC0415

    server_main()


__all__ = ["main"]

if __name__ == "__main__":
    main()
