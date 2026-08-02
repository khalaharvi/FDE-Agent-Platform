"""Tests for `fde_training.rewards.outcome` (`r_outcome`,
`r_retrieval_quality`).

Ported from the pre-split `training/test_grpo_rewards.py`; every assertion
below is unchanged from that file.
"""

from __future__ import annotations

import json

import pytest

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


KG_SEARCH_RESULT = {
    "results": [
        {"node_key": "ctrl:discount_threshold", "node_type": "control", "rrf_score": 0.03},
        {"node_key": "act:manager_approval", "node_type": "activity", "rrf_score": 0.02},
    ],
    "returned": 2,
}


# ===========================================================================
# r_outcome
# ===========================================================================
class TestROutcome:
    def test_exact_match_f1_is_1(self):
        episode = [assistant_answer("The answer is [ctrl:discount_threshold].")]
        rewards = gr.r_outcome([episode], gold_keys=[["ctrl:discount_threshold"]])
        assert rewards == [1.0]

    def test_no_overlap_f1_is_0(self):
        episode = [assistant_answer("The answer is [wrong:key].")]
        rewards = gr.r_outcome([episode], gold_keys=[["ctrl:discount_threshold"]])
        assert rewards == [0.0]

    def test_partial_overlap_f1(self):
        # predicted={"a","b"}, gold={"a","c"} -> precision=0.5, recall=0.5, f1=0.5
        episode = [assistant_answer("[a] and [b]")]
        rewards = gr.r_outcome([episode], gold_keys=[["a", "c"]])
        assert rewards == [0.5]

    def test_missing_gold_keys_kwarg_defaults_empty(self):
        episode = [assistant_answer("no citations here")]
        assert gr.r_outcome([episode]) == [1.0]  # both predicted and gold empty

    def test_per_example_gold_keys_list(self):
        e1 = [assistant_answer("[a]")]
        e2 = [assistant_answer("[b]")]
        rewards = gr.r_outcome([e1, e2], gold_keys=[["a"], ["z"]])
        assert rewards == [1.0, 0.0]


# ===========================================================================
# r_retrieval_quality -- nDCG@k
# ===========================================================================
class TestRRetrievalQuality:
    def test_perfect_ranking_scores_1(self):
        episode = [
            assistant_call("c1", "kg_search", {"query": "x"}),
            tool_result("c1", "kg_search", KG_SEARCH_RESULT),
        ]
        rewards = gr.r_retrieval_quality(
            [episode], relevant_keys=[["ctrl:discount_threshold", "act:manager_approval"]]
        )
        assert rewards[0] == pytest.approx(1.0)

    def test_relevant_item_ranked_lower_scores_less_than_perfect(self):
        swapped_result = {"results": [dict(r) for r in reversed(KG_SEARCH_RESULT["results"])]}
        episode = [
            assistant_call("c1", "kg_search", {"query": "x"}),
            tool_result("c1", "kg_search", swapped_result),
        ]
        rewards = gr.r_retrieval_quality([episode], relevant_keys=[["ctrl:discount_threshold"]])
        assert 0.0 < rewards[0] < 1.0

    def test_no_relevant_hits_scores_0(self):
        episode = [
            assistant_call("c1", "kg_search", {"query": "x"}),
            tool_result("c1", "kg_search", KG_SEARCH_RESULT),
        ]
        rewards = gr.r_retrieval_quality([episode], relevant_keys=[["totally:unrelated"]])
        assert rewards[0] == 0.0

    def test_missing_relevant_keys_is_neutral(self):
        episode = [
            assistant_call("c1", "kg_search", {"query": "x"}),
            tool_result("c1", "kg_search", KG_SEARCH_RESULT),
        ]
        assert gr.r_retrieval_quality([episode]) == [1.0]
