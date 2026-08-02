"""Tests for `fde_training.rewards.efficiency` (`r_hop_efficiency`,
`r_cost`).

Ported from the pre-split `training/test_grpo_rewards.py`; every assertion
below is unchanged from that file.
"""

from __future__ import annotations

import json

from fde_training import rewards as gr


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


# ===========================================================================
# r_hop_efficiency -- two-sided
# ===========================================================================
class TestRHopEfficiency:
    def test_zero_tool_calls_is_worst(self):
        episode = [assistant_answer("guessing without retrieval")]
        assert gr.r_hop_efficiency([episode], hops_required=[2]) == [0.0]

    def test_calls_within_ideal_band_scores_1(self):
        episode = [
            assistant_call("c1", "kg_search", {"query": "a"}),
            tool_result("c1", "kg_search", {"results": []}),
            assistant_call("c2", "kg_traverse", {"start_keys": ["x"]}),
            tool_result("c2", "kg_traverse", {"nodes": []}),
        ]
        rewards = gr.r_hop_efficiency([episode], hops_required=[2])
        assert rewards == [1.0]

    def test_under_retrieval_relative_to_hops_required_is_penalised(self):
        episode = [
            assistant_call("c1", "kg_search", {"query": "a"}),
            tool_result("c1", "kg_search", {"results": []}),
        ]
        rewards = gr.r_hop_efficiency([episode], hops_required=[4])
        assert 0.0 < rewards[0] < 1.0

    def test_excessive_calls_penalised_but_not_zero(self):
        calls = []
        for i in range(12):
            calls.append(assistant_call(f"c{i}", "kg_traverse", {"start_keys": [f"x{i}"]}))
            calls.append(tool_result(f"c{i}", "kg_traverse", {"nodes": []}))
        rewards = gr.r_hop_efficiency([calls], hops_required=[2])
        assert 0.0 <= rewards[0] < 1.0

    def test_adjacent_duplicate_calls_penalised(self):
        dup_args = {"query": "same question", "k": 5}
        episode = [
            assistant_call("c1", "kg_search", dup_args),
            tool_result("c1", "kg_search", {"results": []}),
            assistant_call("c2", "kg_search", dup_args),
            tool_result("c2", "kg_search", {"results": []}),
        ]
        distinct = [
            assistant_call("c1", "kg_search", {"query": "a"}),
            tool_result("c1", "kg_search", {"results": []}),
            assistant_call("c2", "kg_traverse", {"start_keys": ["x"]}),
            tool_result("c2", "kg_traverse", {"nodes": []}),
        ]
        r_dup, r_distinct = gr.r_hop_efficiency([episode, distinct], hops_required=[2, 2])
        assert r_dup < r_distinct

    def test_never_rewards_fewer_calls_over_more_when_both_under_target(self):
        """Anti-collapse invariant: within the under-retrieval regime, MORE
        tool calls must never score lower than fewer -- this is precisely
        the property whose violation causes the documented GRPO collapse."""
        one_call = [
            assistant_call("c1", "kg_search", {"query": "a"}),
            tool_result("c1", "kg_search", {"results": []}),
        ]
        two_calls = [
            assistant_call("c1", "kg_search", {"query": "a"}),
            tool_result("c1", "kg_search", {"results": []}),
            assistant_call("c2", "kg_traverse", {"start_keys": ["x"]}),
            tool_result("c2", "kg_traverse", {"nodes": []}),
        ]
        r_one, r_two = gr.r_hop_efficiency([one_call, two_calls], hops_required=[4, 4])
        assert r_two >= r_one


# ===========================================================================
# r_cost
# ===========================================================================
class TestRCost:
    def test_cheap_episode_small_penalty(self):
        episode = [assistant_answer("short")]
        rewards = gr.r_cost([episode])
        assert -0.2 <= rewards[0] <= 0.0

    def test_expensive_episode_larger_penalty_but_bounded(self):
        big_result = {"results": [{"node_key": f"n{i}", "summary": "x" * 500} for i in range(50)]}
        episode = [
            assistant_call("c1", "kg_search", {"query": "x"}),
            tool_result("c1", "kg_search", big_result),
        ]
        rewards = gr.r_cost([episode])
        assert -1.0 <= rewards[0] < -0.05

    def test_cost_never_exceeds_bound(self):
        huge_result = {
            "results": [{"node_key": f"n{i}", "summary": "x" * 5000} for i in range(500)]
        }
        episode = [
            assistant_call("c1", "kg_search", {"query": "x"}),
            tool_result("c1", "kg_search", huge_result),
        ]
        rewards = gr.r_cost([episode])
        assert rewards[0] >= -1.0

    def test_cheaper_episode_scores_higher_than_expensive_one(self):
        cheap = [assistant_answer("ok")]
        expensive_result = {
            "results": [{"node_key": f"n{i}", "summary": "x" * 1000} for i in range(80)]
        }
        expensive = [
            assistant_call("c1", "kg_search", {"query": "x"}),
            tool_result("c1", "kg_search", expensive_result),
        ]
        r_cheap, r_expensive = gr.r_cost([cheap, expensive])
        assert r_cheap > r_expensive
