"""Tests for the replay adapter itself: parsing, resume, and bad lines.

Everything here is local-file only. The `s3://` branch differs from the local
one solely in where the line iterator comes from, and exercising it would mean
either a live bucket or a boto3 mock detailed enough that it tests the mock.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from fde_sor.adapters.replay import ReplayAdapter


def _write(path: Path, lines: list[str]) -> str:
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return str(path)


async def _collect(adapter: ReplayAdapter, cursor: str | None = None) -> list[Any]:
    return [record async for record in adapter.fetch(cursor)]


async def test_each_line_becomes_one_record(tmp_path: Path) -> None:
    uri = _write(tmp_path / "e.jsonl", [json.dumps({"id": i}) for i in range(3)])
    records = await _collect(ReplayAdapter(uri))
    assert [r.payload["id"] for r in records] == [0, 1, 2]


async def test_the_cursor_is_the_line_number(tmp_path: Path) -> None:
    """Line numbers are what make a timed-out backfill resumable instead of
    restartable.
    """
    uri = _write(tmp_path / "e.jsonl", [json.dumps({"id": i}) for i in range(3)])
    records = await _collect(ReplayAdapter(uri))
    assert [r.cursor for r in records] == ["0", "1", "2"]
    assert [r.ref for r in records] == ["0", "1", "2"]


async def test_a_numeric_cursor_resumes_after_that_line(tmp_path: Path) -> None:
    uri = _write(tmp_path / "e.jsonl", [json.dumps({"id": i}) for i in range(5)])
    records = await _collect(ReplayAdapter(uri), cursor="2")
    assert [r.payload["id"] for r in records] == [3, 4]


async def test_a_non_numeric_cursor_restarts_from_the_beginning(tmp_path: Path) -> None:
    """A rest_poll adapter's watermark is an ISO timestamp, not a line number.
    Restarting is safe -- the dedup index makes the re-read a no-op -- and much
    better than crashing a backfill on a stale cursor.
    """
    uri = _write(tmp_path / "e.jsonl", [json.dumps({"id": i}) for i in range(3)])
    records = await _collect(ReplayAdapter(uri), cursor="2026-03-01T00:00:00Z")
    assert len(records) == 3


async def test_blank_lines_are_skipped_silently(tmp_path: Path) -> None:
    uri = _write(tmp_path / "e.jsonl", [json.dumps({"id": 0}), "", "   ", json.dumps({"id": 1})])
    records = await _collect(ReplayAdapter(uri))
    assert [r.payload["id"] for r in records] == [0, 1]


async def test_unparseable_lines_are_skipped_and_the_rest_still_land(tmp_path: Path) -> None:
    uri = _write(
        tmp_path / "e.jsonl",
        [json.dumps({"id": 0}), "{not json", json.dumps([1, 2]), json.dumps({"id": 1})],
    )
    records = await _collect(ReplayAdapter(uri))
    assert [r.payload["id"] for r in records] == [0, 1], (
        "a corrupt line in a 600k-line export must not cost the whole backfill"
    )


async def test_an_empty_file_yields_nothing(tmp_path: Path) -> None:
    path = tmp_path / "empty.jsonl"
    path.write_text("", encoding="utf-8")
    assert await _collect(ReplayAdapter(str(path))) == []


async def test_a_malformed_s3_uri_is_rejected() -> None:
    with pytest.raises(ValueError, match="s3://bucket/key"):
        await _collect(ReplayAdapter("s3://bucket-with-no-key"))
