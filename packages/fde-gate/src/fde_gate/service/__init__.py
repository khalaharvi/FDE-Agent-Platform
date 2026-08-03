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

The not-found envelope
----------------------
A read that finds nothing returns `{"error": "..."}` rather than raising, so
the caller decides whether that is a 404 page or a 404 body. `is_missing`
below is how a caller tests for it; `"error" in result` is not.
"""

from __future__ import annotations

from typing import Any

__all__ = ["drift", "is_missing", "proposals", "reviewers", "runs", "sources", "workflows"]


def is_missing(result: dict[str, Any], key: str) -> bool:
    """Is this the not-found envelope rather than the thing that was asked for?

    `key` is a key the SUCCESS shape always carries and the envelope never
    does -- `proposal_id` for a proposal, `workflow` for a playbook. The
    discrimination has to be that way round because the envelope's own key is
    `error`, and `error` is also a column name: `wf.run` has one, so every
    real run carried an `error` key and `"error" in result` was true for all
    of them. Both callers of `get_run` concluded "not found" for every run
    that existed (`runs.is_missing`, and the test that pins it). Nothing
    stopped the same collision arriving on any other read, since the tables
    these functions select from are free to grow a column called anything.

    Testing for the positive key cannot fail that way: a result that has the
    key is the thing, and one that does not is the envelope, whatever else
    either of them happens to contain.
    """
    return key not in result
