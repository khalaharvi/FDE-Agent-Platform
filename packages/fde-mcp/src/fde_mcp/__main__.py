"""`python -m fde_mcp` entrypoint. Also the target of the `fde-mcp` console
script (see pyproject.toml) -- both invocation paths end up calling the
exact same `fde_mcp.server.main`, so there is exactly one place that decides
transport/host/port for a locally-run server versus one installed as a
package.
"""

from __future__ import annotations

from fde_mcp.server import main

__all__ = ["main"]

if __name__ == "__main__":
    main()
