"""Tests for `fde_training.rewards.validity` (`r_format`, `r_schema_valid`)
plus the composite `compose_rewards`/`DEFAULT_WEIGHTS` machinery in
`fde_training.rewards.__init__` (kept here rather than a dedicated file
because composing the eight terms is, in effect, an end-to-end exercise of
every gate `validity.py` defines).

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


def grounded_episode():
    return [
        {"role": "user", "content": "what gates approval?"},
        assistant_call(
            "c1", "kg_search", {"engagement_id": "e1", "query": "approval control", "k": 5}
        ),
        tool_result("c1", "kg_search", KG_SEARCH_RESULT),
        assistant_answer("Manager approval is gated by [ctrl:discount_threshold]."),
    ]


# ===========================================================================
# r_format
# ===========================================================================
class TestRFormat:
    def test_well_formed_tool_call_scores_1(self):
        assert gr.r_format([grounded_episode()]) == [1.0]

    def test_no_tool_calls_is_neutral(self):
        episode = [{"role": "user", "content": "hi"}, assistant_answer("hello")]
        assert gr.r_format([episode]) == [1.0]

    def test_unknown_tool_name_penalised(self):
        episode = [
            assistant_call("c1", "not_a_real_tool", {}),
            tool_result("c1", "not_a_real_tool", {}),
        ]
        assert gr.r_format([episode]) == [0.0]

    def test_malformed_json_arguments_penalised(self):
        bad_call = {
            "id": "c1",
            "type": "function",
            "function": {"name": "kg_search", "arguments": "{not json"},
        }
        episode = [{"role": "assistant", "content": None, "tool_calls": [bad_call]}]
        assert gr.r_format([episode]) == [0.0]

    def test_mixed_batch_partial_credit(self):
        good = grounded_episode()
        bad = [assistant_call("c1", "bogus", {}), tool_result("c1", "bogus", {})]
        assert gr.r_format([good, bad]) == [1.0, 0.0]


# ===========================================================================
# r_schema_valid
# ===========================================================================
class TestRSchemaValid:
    def test_valid_args_scores_1(self):
        assert gr.r_schema_valid([grounded_episode()]) == [1.0]

    def test_max_hops_over_ceiling_penalised(self):
        episode = [
            assistant_call("c1", "kg_traverse", {"start_keys": ["x"], "max_hops": 7}),
            tool_result("c1", "kg_traverse", {"nodes": []}),
        ]
        assert gr.r_schema_valid([episode]) == [0.0]

    def test_max_hops_at_ceiling_ok(self):
        episode = [
            assistant_call("c1", "kg_traverse", {"start_keys": ["x"], "max_hops": 6}),
            tool_result("c1", "kg_traverse", {"nodes": []}),
        ]
        assert gr.r_schema_valid([episode]) == [1.0]

    def test_invalid_node_type_penalised(self):
        episode = [
            assistant_call("c1", "kg_search", {"query": "q", "node_types": ["not_a_type"]}),
            tool_result("c1", "kg_search", {"results": []}),
        ]
        assert gr.r_schema_valid([episode]) == [0.0]

    def test_kg_propose_invalid_node_type_penalised(self):
        episode = [
            assistant_call(
                "c1",
                "kg_propose",
                {"items": [{"op": "add_node", "node_type": "not_a_type", "subject_key": "x"}]},
            ),
            tool_result("c1", "kg_propose", {}),
        ]
        assert gr.r_schema_valid([episode]) == [0.0]

    def test_kg_propose_valid_node_type_ok(self):
        episode = [
            assistant_call(
                "c1",
                "kg_propose",
                {
                    "items": [
                        {
                            "op": "add_node",
                            "node_type": "pain_point",
                            "subject_key": "x",
                            "agent_confidence": 0.5,
                        }
                    ]
                },
            ),
            tool_result("c1", "kg_propose", {}),
        ]
        assert gr.r_schema_valid([episode]) == [1.0]


# ===========================================================================
# compose_rewards
# ===========================================================================
class TestComposeRewards:
    def test_weights_sum_and_apply(self):
        combined, per_term = gr.compose_rewards([grounded_episode()])
        assert set(per_term.keys()) == set(gr.DEFAULT_WEIGHTS.keys())
        expected = sum(gr.DEFAULT_WEIGHTS[k] * per_term[k][0] for k in per_term)
        assert combined[0] == pytest.approx(expected)

    def test_grounded_beats_hallucinated_on_combined_score(self):
        hallucinated = [
            assistant_call("c1", "kg_search", {"query": "x"}),
            tool_result("c1", "kg_search", KG_SEARCH_RESULT),
            assistant_answer("Owned by [role:made_up_role]."),
        ]
        combined, _ = gr.compose_rewards(
            [grounded_episode(), hallucinated], gold_keys=[["ctrl:discount_threshold"], []]
        )
        assert combined[0] > combined[1]

    def test_custom_weights_override_defaults(self):
        combined_default, _ = gr.compose_rewards([grounded_episode()])
        zero_weights = dict.fromkeys(gr.DEFAULT_WEIGHTS, 0.0)
        combined_zero, _ = gr.compose_rewards([grounded_episode()], weights=zero_weights)
        assert combined_zero == [0.0]
        assert combined_default[0] != 0.0

    def test_mismatched_length_raises(self):
        class BadFn:
            __name__ = "bad"

            def __call__(self, completions, **kwargs):
                return [0.0]  # wrong length for 2 completions

        bad_registry = dict(gr.REWARD_FUNCTIONS)
        bad_registry["r_format"] = BadFn()
        orig = gr.REWARD_FUNCTIONS
        gr.REWARD_FUNCTIONS = bad_registry
        try:
            with pytest.raises(ValueError, match="reward fn"):
                gr.compose_rewards([grounded_episode(), grounded_episode()])
        finally:
            gr.REWARD_FUNCTIONS = orig
