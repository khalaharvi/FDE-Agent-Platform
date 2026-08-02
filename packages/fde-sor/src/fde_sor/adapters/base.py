"""adapters/base.py -- the adapter contract.

Three types and two protocols, which is the whole surface an adapter author
has to learn.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, ClassVar, Protocol, runtime_checkable

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

__all__ = ["AckingAdapter", "AdapterRow", "RawRecord", "SorAdapter"]


@dataclass(frozen=True, slots=True)
class AdapterRow:
    """One `sor.adapter` row, as loaded by `fde_sor.registry`.

    A frozen snapshot rather than a live handle: an adapter must not be able
    to edit its own registration, and `fde_ingest`'s grant on `sor.adapter`
    (SELECT + UPDATE, no INSERT -- db/010:57) allows exactly one write, the
    cursor advance, which `observations.ingest` performs directly.
    """

    adapter_id: int
    engagement_id: str
    adapter_key: str
    system_node_key: str
    kind: str
    secret_arn: str | None
    mapping: dict[str, Any]
    last_cursor: str | None
    poll_cron: str | None = None
    is_active: bool = True


@dataclass(frozen=True, slots=True)
class RawRecord:
    """One record as the source system produced it, before any mapping.

    Attributes:
        payload: the raw record. Interpreted only by `mapping.normalize`.
        cursor: the watermark this record advances the adapter to, or `None`
            for a source where the watermark is not record-derived (SQS, where
            delivery order is not a total order over event time). The pipeline
            commits the cursor of the LAST record in a batch together with
            that batch's inserts.
        ref: an opaque per-record handle -- an SQS messageId, a JSONL line
            number, an LSN -- carried through so a record that fails to
            normalise can be attributed back to its source. This is what lets
            the SQS Lambda return precise `batchItemFailures` instead of
            failing (and therefore redelivering) a whole batch of ten because
            one message was malformed.
    """

    payload: dict[str, Any]
    cursor: str | None = None
    ref: str | None = None


class SorAdapter(Protocol):
    """What every adapter kind implements. Deliberately one method."""

    kind: ClassVar[str]

    def fetch(self, cursor: str | None) -> AsyncIterator[RawRecord]:
        """Yield records newer than `cursor`, oldest first where the source
        has an order. `cursor` is `sor.adapter.last_cursor`, or `None` on the
        very first run (each adapter decides its own cold-start window; the
        polling ones use `FDE_SOR_LOOKBACK_DAYS`).
        """
        ...


@runtime_checkable
class AckingAdapter(Protocol):
    """An adapter whose source must be told, separately, that a batch is safe
    to forget.

    Only `db_cdc` needs this today: logical decoding is peek-then-advance, and
    advancing the replication slot before the observations are committed in
    the platform database would lose records that a crash then made
    unrecoverable. `observations.ingest` calls this AFTER each batch commits,
    which makes the pipeline at-least-once and the `dedup_key` index
    (db/014) makes it effectively exactly-once.
    """

    async def on_batch_committed(self, cursor: str | None) -> None: ...
