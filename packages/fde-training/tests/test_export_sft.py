"""Tests for `fde_training.export_sft`'s pure logic: mask-integrity
validation, adjacent-duplicate-retrieval collapsing, split resolution, and
record building. No live Postgres needed -- `fetch_rows`/
`build_preference_pairs` (the two DB-touching functions) are exercised only
by `requires_db`-marked integration tests elsewhere, per this package's
`conftest.py`.
"""

from __future__ import annotations

import pytest

from fde_training.export_sft import (
    MaskIntegrityError,
    _collapse_duplicate_retrievals,
    build_records,
    resolve_split,
    validate_message_masks,
)


def _row(
    *,
    session_id: str = "s1",
    turn: int = 1,
    role: str = "assistant",
    content: str | None = None,
    tool_calls: list | None = None,
    tool_call_id: str | None = None,
    tool_name: str | None = None,
    tool_result: dict | None = None,
    trainable: bool = True,
    grounded: bool | None = True,
    agent_name: str = "engagement",
    task_kind: str = "map_workflow",
    outcome: str = "accepted",
    db_split: str | None = None,
    base_commit_id: int = 1,
) -> dict:
    return {
        "session_id": session_id,
        "agent_name": agent_name,
        "task_kind": task_kind,
        "db_split": db_split,
        "base_commit_id": base_commit_id,
        "outcome": outcome,
        "turn": turn,
        "role": role,
        "content": content,
        "tool_calls": tool_calls,
        "tool_call_id": tool_call_id,
        "tool_name": tool_name,
        "tool_result": tool_result,
        "trainable": trainable,
        "grounded": grounded,
    }


def _tool_call(call_id: str, name: str, arguments: str) -> list[dict]:
    return [{"id": call_id, "type": "function", "function": {"name": name, "arguments": arguments}}]


# ===========================================================================
# validate_message_masks -- THE invariant this module exists to enforce
# ===========================================================================
class TestValidateMessageMasks:
    def test_correct_masks_pass(self):
        messages = [
            {"role": "system", "trainable": False},
            {"role": "user", "trainable": False},
            {"role": "assistant", "trainable": True},
            {"role": "tool", "trainable": False},
            {"role": "assistant", "trainable": True},
        ]
        validate_message_masks("s1", messages)  # must not raise

    def test_tool_message_marked_trainable_raises(self):
        messages = [{"role": "tool", "trainable": True}]
        with pytest.raises(MaskIntegrityError, match="expected False"):
            validate_message_masks("s1", messages)

    def test_user_message_marked_trainable_raises(self):
        messages = [{"role": "user", "trainable": True}]
        with pytest.raises(MaskIntegrityError, match="expected False"):
            validate_message_masks("s1", messages)

    def test_assistant_message_marked_untrainable_raises(self):
        messages = [{"role": "assistant", "trainable": False}]
        with pytest.raises(MaskIntegrityError, match="expected True"):
            validate_message_masks("s1", messages)


# ===========================================================================
# _collapse_duplicate_retrievals
# ===========================================================================
class TestCollapseDuplicateRetrievals:
    def test_no_messages_is_a_noop(self):
        out, n = _collapse_duplicate_retrievals([])
        assert out == []
        assert n == 0

    def test_adjacent_identical_retrieval_is_collapsed(self):
        messages = [
            {
                "role": "assistant",
                "trainable": True,
                "tool_calls": _tool_call("c1", "kg_search", '{"k":5}'),
            },
            {"role": "tool", "trainable": False, "content": '{"results":[]}'},
            {
                "role": "assistant",
                "trainable": True,
                "tool_calls": _tool_call("c2", "kg_search", '{"k":5}'),
            },
            {"role": "tool", "trainable": False, "content": '{"results":[]}'},
            {"role": "assistant", "trainable": True, "content": "done"},
        ]
        out, n = _collapse_duplicate_retrievals(messages)
        assert n == 1
        # Only the first (call, result) pair and the final answer survive.
        assert len(out) == 3
        assert out[-1]["content"] == "done"

    def test_non_adjacent_repeat_is_not_collapsed(self):
        messages = [
            {
                "role": "assistant",
                "trainable": True,
                "tool_calls": _tool_call("c1", "kg_search", '{"k":5}'),
            },
            {"role": "tool", "trainable": False, "content": '{"results":[]}'},
            {
                "role": "assistant",
                "trainable": True,
                "tool_calls": _tool_call("c2", "kg_traverse", '{"start_keys":["x"]}'),
            },
            {"role": "tool", "trainable": False, "content": '{"nodes":[]}'},
            {
                "role": "assistant",
                "trainable": True,
                "tool_calls": _tool_call("c3", "kg_search", '{"k":5}'),
            },
            {"role": "tool", "trainable": False, "content": '{"results":[]}'},
        ]
        out, n = _collapse_duplicate_retrievals(messages)
        assert n == 0
        assert len(out) == len(messages)

    def test_different_arguments_are_not_collapsed(self):
        messages = [
            {
                "role": "assistant",
                "trainable": True,
                "tool_calls": _tool_call("c1", "kg_search", '{"k":5}'),
            },
            {"role": "tool", "trainable": False, "content": '{"results":[]}'},
            {
                "role": "assistant",
                "trainable": True,
                "tool_calls": _tool_call("c2", "kg_search", '{"k":10}'),
            },
            {"role": "tool", "trainable": False, "content": '{"results":[]}'},
        ]
        out, n = _collapse_duplicate_retrievals(messages)
        assert n == 0
        assert len(out) == 4


# ===========================================================================
# resolve_split
# ===========================================================================
class TestResolveSplit:
    @pytest.mark.parametrize("db_split", ["train", "validation", "test"])
    def test_prefers_already_set_split(self, db_split):
        assert resolve_split(db_split, "any-session-id") == db_split

    def test_holdout_folds_into_test(self):
        assert resolve_split("holdout", "any-session-id") == "test"

    def test_none_falls_back_to_deterministic_hash(self):
        result = resolve_split(None, "session-abc")
        assert result in ("train", "validation", "test")

    def test_deterministic_across_calls(self):
        first = resolve_split(None, "session-xyz")
        second = resolve_split(None, "session-xyz")
        assert first == second


# ===========================================================================
# build_records
# ===========================================================================
class TestBuildRecords:
    def test_single_session_round_trips(self):
        rows_by_session = {
            "s1": [
                _row(turn=1, role="system", content="You are the agent.", trainable=False),
                _row(turn=2, role="user", content="What gates approval?", trainable=False),
                _row(
                    turn=3,
                    role="assistant",
                    content=None,
                    tool_calls=_tool_call("c1", "kg_search", '{"query": "approval"}'),
                ),
                _row(
                    turn=4,
                    role="tool",
                    tool_call_id="c1",
                    tool_name="kg_search",
                    tool_result={"results": [{"node_key": "ctrl:x"}]},
                    trainable=False,
                ),
                _row(turn=5, role="assistant", content="Gated by [ctrl:x]."),
            ]
        }
        records, stats = build_records(rows_by_session)
        assert stats["sessions_seen"] == 1
        assert stats["sessions_exported"] == 1
        assert len(records) == 1
        record = records[0]
        assert record["meta"]["session_id"] == "s1"
        assert record["assistant_mask"] == [False, False, True, False, True]

    def test_mask_failure_raises_and_is_not_exported(self):
        rows_by_session = {"s1": [_row(turn=1, role="tool", trainable=True)]}
        with pytest.raises(MaskIntegrityError):
            build_records(rows_by_session)

    def test_duplicate_trajectory_across_sessions_is_deduped(self):
        session_rows = [
            _row(turn=1, role="assistant", tool_calls=_tool_call("c1", "kg_search", '{"q":1}')),
            _row(
                turn=2,
                role="tool",
                tool_call_id="c1",
                tool_name="kg_search",
                tool_result={"results": []},
                trainable=False,
            ),
            _row(turn=3, role="assistant", content="done"),
        ]
        # Two sessions with byte-identical tool-call sequences -- "s1" wins
        # because build_records sorts session ids and keeps the first.
        rows_by_session = {
            "s1": [dict(r, session_id="s1") for r in session_rows],
            "s2": [dict(r, session_id="s2") for r in session_rows],
        }
        records, stats = build_records(rows_by_session)
        assert stats["sessions_dropped_as_duplicate_trajectory"] == 1
        assert len(records) == 1
        assert records[0]["meta"]["session_id"] == "s1"

    def test_split_assignment_prefers_db_split(self):
        rows_by_session = {
            "s1": [_row(turn=1, role="assistant", content="done", db_split="validation")],
        }
        records, _ = build_records(rows_by_session)
        assert records[0]["meta"]["split"] == "validation"

    def test_fully_grounded_meta_reflects_grounded_column(self):
        rows_by_session = {
            "s1": [
                _row(turn=1, role="assistant", content="a", grounded=True),
                _row(turn=2, role="assistant", content="b", grounded=False),
            ]
        }
        records, _ = build_records(rows_by_session)
        assert records[0]["meta"]["fully_grounded"] is False
