"""Tests for `fde_training.rewards.monitor.RewardHackingMonitor`.

Ported from the pre-split `training/test_grpo_rewards.py`. The original
tests asserted the literal string "REWARD HACKING ALERT" appeared in
`caplog.text`; `monitor.py` now logs a structured event through
`fde_mcp.logging.get_logger` instead of the stdlib `logging` module
directly, but the event string itself still starts with "REWARD HACKING
ALERT" (see `monitor.py`), and `caplog` still captures it -- so this
assertion, and every other one from the original suite, is unchanged.
"""

from __future__ import annotations

import json

import pytest

from fde_training.rewards.monitor import RewardHackingMonitor


def tool_call(call_id: str, name: str, args: dict) -> dict:
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(args)},
    }


def tool_result(call_id: str, name: str, result: dict) -> dict:
    return {"role": "tool", "tool_call_id": call_id, "name": name, "content": json.dumps(result)}


def assistant_call(call_id: str, name: str, args: dict) -> dict:
    return {"role": "assistant", "content": None, "tool_calls": [tool_call(call_id, name, args)]}


def assistant_answer(text: str) -> dict:
    return {"role": "assistant", "content": text}


class TestRewardHackingMonitor:
    def _episode_with_n_calls(self, n: int) -> list:
        out = []
        for i in range(n):
            out.append(assistant_call(f"c{i}", "kg_search", {"query": f"q{i}"}))
            out.append(tool_result(f"c{i}", "kg_search", {"results": []}))
        out.append(assistant_answer("done"))
        return out

    def test_stable_tool_calls_no_collapse_warning(self, caplog):
        monitor = RewardHackingMonitor(collapse_window=3)
        for step in range(6):
            monitor.observe(step, [self._episode_with_n_calls(3)])
        assert "REWARD HACKING ALERT" not in caplog.text

    def test_monotonic_decline_triggers_alert(self, caplog):
        caplog.set_level("ERROR", logger="fde_training.rewards.monitor")
        monitor = RewardHackingMonitor(collapse_window=3)
        declining_call_counts = [5, 4, 3, 2, 1]
        for step, n in enumerate(declining_call_counts):
            monitor.observe(step, [self._episode_with_n_calls(n)])
        assert "REWARD HACKING ALERT" in caplog.text

    def test_strict_mode_raises_on_collapse(self):
        monitor = RewardHackingMonitor(collapse_window=2, strict=True)
        monitor.observe(0, [self._episode_with_n_calls(4)])
        monitor.observe(1, [self._episode_with_n_calls(3)])
        with pytest.raises(RuntimeError, match="tool-call collapse detected"):
            monitor.observe(2, [self._episode_with_n_calls(2)])

    def test_summary_reports_latest_stats(self):
        monitor = RewardHackingMonitor()
        monitor.observe(0, [self._episode_with_n_calls(3)])
        summary = monitor.summary()
        assert summary["steps_recorded"] == 1
        assert summary["latest_mean_tool_calls"] == 3.0
