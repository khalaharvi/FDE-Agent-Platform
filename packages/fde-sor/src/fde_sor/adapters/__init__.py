"""fde_sor.adapters -- one module per `sor.adapter.kind`.

Every module here implements exactly one thing: `fetch(cursor)`, an async
iterator of `RawRecord`. Mapping, actor hashing, the idempotent insert, and
the cursor advance all live in `fde_sor.observations` and are shared verbatim,
because `kind` "only tells the platform how the adapter is invoked ... not how
its `mapping` is interpreted" (docs/08 §2.4).
"""

from __future__ import annotations

from fde_sor.adapters.base import AckingAdapter, AdapterRow, RawRecord, SorAdapter

__all__ = ["AckingAdapter", "AdapterRow", "RawRecord", "SorAdapter"]
