"""service/ -- one module per request domain, split by the DB ROLE it uses.

`proposals`, `reviewers`, `sources` and `workflows.publish` run as
`fde_gate_service`; `workflows` listing, `runs` and `drift` run as
`fde_prodops`. That is the whole reason these are six modules and not one
file of functions: the split makes the privilege boundary visible in the
import graph, so adding a run endpoint to `proposals.py` looks as wrong as
it is.

`reviewers` and `sources` are the two modules that authorise before they act
-- they write tables no plpgsql function guards, so the check is Python's
job there and the database's everywhere else. Their headers say why, and why
one demands an administrator while the other only demands an active
reviewer.

Every function here is a thin caller of the SQL in db/013, except `sources`,
whose SQL is shared with the MCP evidence tools and therefore lives in
`fde_mcp.ingest`. None of them flips a status with a bare UPDATE -- see
db/013's header for why the transitions live in the database.
"""

from __future__ import annotations

__all__ = ["drift", "proposals", "reviewers", "runs", "sources", "workflows"]
