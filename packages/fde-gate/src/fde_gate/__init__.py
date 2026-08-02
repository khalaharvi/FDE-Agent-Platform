"""fde_gate -- the gate service, the prod-ops console, and the workflow runner.

One AWS Lambda behind one API Gateway HTTP API. It is the only component in
the platform that may merge a proposal into the graph, publish a workflow, or
move a workflow run, and it holds those three authorities because it is the
only component a human is ever on the other end of.

What lives where
----------------
* `config`     -- every `FDE_GATE_*` variable, declared once (docs/11 §4).
* `http`       -- the router, APIGW v2 event parsing, and the Postgres-error
                  to HTTP-status contract (docs/11 §7).
* `handler`    -- the Lambda entrypoint; dispatches APIGW / EventBridge /
                  self-invoke events.
* `service/*`  -- one module per request domain, each a thin caller of the
                  SQL transitions in db/013. No status is ever flipped from
                  Python with a bare UPDATE.
* `runner`     -- the two-transaction step loop; `executors` does the work.
* `ui`         -- the same service functions rendered as server-side HTML.
* `deploy/*`   -- the `fde-gate-deploy` provisioning CLI.

The database roles
------------------
The process connects with the same owner-adjacent credential the MCP server
uses (`FDE_DB_*`) and downgrades per request domain via `SET LOCAL ROLE`:
`fde_gate_service` for proposals, review, merge, publish, labels and expiry;
`fde_prodops` for workflow runs and drift triage. The credential is a
ceiling; the role is the boundary Postgres actually enforces. See
`fde_mcp.db`'s module docstring, which this package reuses wholesale rather
than re-deriving.
"""

from __future__ import annotations

__all__ = ["__version__"]

__version__ = "0.1.0"
