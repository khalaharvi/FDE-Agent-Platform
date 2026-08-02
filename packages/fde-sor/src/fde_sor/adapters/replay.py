"""adapters/replay.py -- JSONL replay, local or S3.

This is not a toy. It is (a) the deterministic backbone every shared-pipeline
test runs through, because a JSONL file needs no network, no credentials and
no live SoR to reproduce exactly, and (b) the body of `backfill.py`, which is
the real `fde-sor-backfill` Lambda behind the AgentCore Gateway's lambda
target -- a customer's historical export IS a JSONL file in S3.

`cursor` is the 0-based line number, so a backfill that dies 400k lines into a
600k-line export resumes at line 400k rather than re-reading the whole object.
Re-read lines are harmless anyway (the dedup index absorbs them); resuming
just makes it fast.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, ClassVar

from fde_mcp.logging import get_logger
from fde_sor.adapters.base import RawRecord
from fde_sor.config import aws_region

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Iterator

__all__ = ["ReplayAdapter"]

log = get_logger(__name__)


def _iter_local_lines(path: str) -> Iterator[str]:
    from pathlib import Path  # noqa: PLC0415 -- only the local branch needs it

    with Path(path).open(encoding="utf-8") as handle:
        yield from handle


def _iter_s3_lines(uri: str) -> Iterator[str]:
    """Stream an `s3://bucket/key` object line by line.

    `iter_lines` on the streaming body rather than `read()`: a warehouse
    export is routinely larger than a Lambda's memory, and the whole point of
    line-cursor resume is that this never needs the object resident.
    """
    import boto3  # noqa: PLC0415 -- only the S3 branch needs it

    without_scheme = uri.removeprefix("s3://")
    bucket, _, key = without_scheme.partition("/")
    if not bucket or not key:
        msg = f"malformed S3 URI {uri!r}; expected s3://bucket/key"
        raise ValueError(msg)
    client = boto3.client("s3", region_name=aws_region())
    body = client.get_object(Bucket=bucket, Key=key)["Body"]
    for raw in body.iter_lines():
        yield raw.decode("utf-8")


class ReplayAdapter:
    """Replays one JSONL source: one raw SoR record per line.

    Blank lines are skipped. A line that is not valid JSON, or is JSON but not
    an object, is skipped with a warning naming the LINE NUMBER -- never the
    line's content, which may contain the actor identifiers this platform goes
    to some length not to log.
    """

    kind: ClassVar[str] = "replay"

    def __init__(self, uri: str) -> None:
        self.uri = uri

    def _lines(self) -> Iterator[str]:
        if self.uri.startswith("s3://"):
            return _iter_s3_lines(self.uri)
        return _iter_local_lines(self.uri)

    async def fetch(self, cursor: str | None) -> AsyncIterator[RawRecord]:
        start_line = 0
        if cursor is not None:
            try:
                start_line = int(cursor) + 1
            except ValueError:
                # A cursor left by a different adapter kind (an ISO timestamp,
                # an LSN) is not a line number. Restarting from 0 is safe --
                # the dedup index makes the re-read a no-op -- and is much
                # better than crashing a backfill on a stale cursor.
                log.warning("replay_cursor_not_a_line_number", uri=self.uri, cursor=cursor)
                start_line = 0

        skipped_bad_lines = 0
        for line_number, line in enumerate(self._lines()):
            if line_number < start_line:
                continue
            text = line.strip()
            if not text:
                continue
            try:
                payload = json.loads(text)
            except json.JSONDecodeError:
                skipped_bad_lines += 1
                log.warning("replay_line_not_json", uri=self.uri, line_number=line_number)
                continue
            if not isinstance(payload, dict):
                skipped_bad_lines += 1
                log.warning("replay_line_not_an_object", uri=self.uri, line_number=line_number)
                continue
            yield RawRecord(payload=payload, cursor=str(line_number), ref=str(line_number))

        if skipped_bad_lines:
            log.warning("replay_lines_skipped", uri=self.uri, skipped=skipped_bad_lines)
