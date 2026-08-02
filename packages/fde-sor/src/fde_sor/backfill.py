"""backfill.py -- historical exports through the live ingestion path.

Thin on purpose. A backfill is not a second ingestion mechanism; it is the
same `observations.ingest` fed by `ReplayAdapter` instead of by a live source.
That equivalence is what makes the seam safe: a backfill covering January and
a poll that starts mid-January produce byte-identical `dedup_key`s for the
records they both cover, so the overlap collapses in the index instead of
double-counting into `detect_control_bypass`'s rate.

This is also the body of the `fde-sor-backfill` Lambda, which is the AgentCore
Gateway's `lambda` target -- the "escape hatch for long-running/batch tools
that should not tie up the MCP server's connection pool" that `gateway.py`
describes. Its parameter list is the contract in
`fde_agents/deploy/schemas/sor_backfill_tool_schema.json`, and a test pins the
two together.

Exports too large for one 15-minute Lambda are split into several S3 objects
and invoked once per object. No Step Functions state machine is introduced for
this; a loop over object keys in the caller is enough, and inventing
orchestration for a job that runs at onboarding would be machinery nobody
maintains. A single object that times out mid-way is simply re-invoked: it
re-reads from line 0 and the dedup index discards everything already landed,
so the retry costs time and nothing else. The line-number cursor is
deliberately NOT persisted to `sor.adapter.last_cursor` -- that column holds
the LIVE source's watermark, and overwriting a Jira poll's `updated >= ...`
timestamp with "line 4999" would make the next poll ask for nonsense.
"""

from __future__ import annotations

import dataclasses
from typing import TYPE_CHECKING

from fde_mcp.logging import get_logger
from fde_sor import registry
from fde_sor.adapters.replay import ReplayAdapter
from fde_sor.observations import ingest

if TYPE_CHECKING:
    from fde_sor.observations import IngestStats

__all__ = ["BACKFILL_PARAMS", "BACKFILL_REQUIRED_PARAMS", "backfill"]

log = get_logger(__name__)

#: The `sor_backfill_observations` tool contract. Declared here, next to the
#: function that implements it, and asserted equal to the packaged Gateway tool
#: schema by `tests/test_gateway_schema.py` -- the placeholder schema that used
#: to live inline in `gateway.py` had drifted from every real handler because
#: nothing connected the two.
BACKFILL_PARAMS: tuple[str, ...] = ("engagement_id", "adapter_key", "s3_uri", "dry_run")
BACKFILL_REQUIRED_PARAMS: tuple[str, ...] = ("engagement_id", "adapter_key", "s3_uri")


async def backfill(
    *, engagement_id: str, adapter_key: str, s3_uri: str, dry_run: bool = False
) -> IngestStats:
    """Replay a JSONL export into `sor.observation` for a registered adapter.

    `s3_uri` may also be a local path, which is what the tests and a local
    onboarding drill use.

    The adapter must already be registered, because the mapping lives on the
    adapter row: a backfill with an ad-hoc mapping supplied at call time would
    be a measurement instrument nobody authored (docs/08 §2.5) and would
    produce observations whose `dedup_key`s do not match the live poll's.
    """
    row = await registry.load_adapter(engagement_id, adapter_key)
    log.info(
        "sor_backfill_starting",
        engagement_id=engagement_id,
        adapter_key=adapter_key,
        adapter_kind=row.kind,
        source=s3_uri,
        dry_run=dry_run,
    )
    # `last_cursor=None` in, `advance_cursor=False` out. The live source's
    # watermark is neither a valid start point for a line-numbered replay nor
    # something a replay may overwrite -- see the module docstring.
    replay_row = dataclasses.replace(row, last_cursor=None)
    return await ingest(ReplayAdapter(s3_uri), replay_row, dry_run=dry_run, advance_cursor=False)
