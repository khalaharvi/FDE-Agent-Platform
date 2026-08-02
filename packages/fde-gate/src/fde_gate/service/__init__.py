"""service/ -- one module per request domain, split by the DB ROLE it uses.

`proposals` and `workflows.publish` run as `fde_gate_service`; `workflows`
listing, `runs` and `drift` run as `fde_prodops`. That is the whole reason
these are four modules and not one file of functions: the split makes the
privilege boundary visible in the import graph, so adding a run endpoint to
`proposals.py` looks as wrong as it is.

Every function here is a thin caller of the SQL in db/013. None of them
flips a status with a bare UPDATE -- see that file's header for why the
transitions live in the database.
"""

from __future__ import annotations

__all__ = ["drift", "proposals", "runs", "workflows"]
