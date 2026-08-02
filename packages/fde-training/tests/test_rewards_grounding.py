"""Tests for `fde_training.rewards.grounding` (`r_grounded`, `r_citation`).

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
# r_grounded -- THE anti-hallucination term
# ===========================================================================
class TestRGrounded:
    def test_fully_grounded_answer_scores_1(self):
        episode = [
            assistant_call(
                "c1", "kg_search", {"engagement_id": "e1", "query": "approval control", "k": 5}
            ),
            tool_result("c1", "kg_search", KG_SEARCH_RESULT),
            assistant_answer("Manager approval is gated by [ctrl:discount_threshold]."),
        ]
        assert gr.r_grounded([episode]) == [1.0]

    def test_hallucinated_citation_scores_0(self):
        episode = [
            assistant_call("c1", "kg_search", {"query": "x"}),
            tool_result("c1", "kg_search", KG_SEARCH_RESULT),
            assistant_answer("The process is owned by [role:made_up_role]."),
        ]
        assert gr.r_grounded([episode]) == [0.0]

    def test_partial_grounding_is_fractional(self):
        episode = [
            assistant_call("c1", "kg_search", {"query": "x"}),
            tool_result("c1", "kg_search", KG_SEARCH_RESULT),
            assistant_answer("See [ctrl:discount_threshold] and also [nonexistent:thing]."),
        ]
        assert gr.r_grounded([episode]) == [0.5]

    def test_answer_with_no_citations_scores_0(self):
        episode = [
            assistant_call("c1", "kg_search", {"query": "x"}),
            tool_result("c1", "kg_search", KG_SEARCH_RESULT),
            assistant_answer("The process is owned by the Legal department, definitely."),
        ]
        assert gr.r_grounded([episode]) == [0.0]

    def test_honest_abstention_scores_1(self):
        episode = [
            assistant_call("c1", "kg_search", {"query": "x"}),
            tool_result("c1", "kg_search", {"results": [], "returned": 0}),
            assistant_answer("No information found for this question."),
        ]
        assert gr.r_grounded([episode]) == [1.0]

    def test_empty_answer_is_neutral_not_penalised(self):
        episode = [
            assistant_call("c1", "kg_search", {"query": "x"}),
            tool_result("c1", "kg_search", KG_SEARCH_RESULT),
        ]
        assert gr.r_grounded([episode]) == [1.0]


# ===========================================================================
# r_citation -- format/presence, independent of correctness
# ===========================================================================
class TestRCitation:
    def test_every_substantive_sentence_cited_scores_1(self):
        episode = [
            assistant_answer("Manager approval is gated by [ctrl:discount_threshold] per policy.")
        ]
        assert gr.r_citation([episode]) == [1.0]

    def test_uncited_substantive_sentence_scores_0(self):
        episode = [assistant_answer("The process is owned by the Legal department entirely.")]
        assert gr.r_citation([episode]) == [0.0]

    def test_short_sentences_exempt(self):
        episode = [assistant_answer("Yes. Correct. Indeed.")]
        assert gr.r_citation([episode]) == [1.0]

    def test_mixed_sentences_partial_credit(self):
        episode = [
            assistant_answer(
                "Manager approval is gated by [ctrl:discount_threshold] per policy. "
                "The whole process was designed five years ago by the finance team."
            )
        ]
        rewards = gr.r_citation([episode])
        assert rewards[0] == pytest.approx(0.5)

    def test_citation_does_not_check_correctness(self):
        # r_citation only checks FORMAT -- a citation to a fabricated key
        # still counts here; r_grounded is what catches the fabrication.
        episode = [assistant_answer("Owned by [totally:made_up_key] per policy review.")]
        assert gr.r_citation([episode]) == [1.0]
