"""Tests for `fde_training.train`.

Everything that can be tested without the `train` extra IS tested without
it -- the dataset conversion, the QA reward adapters, the environment
adapter, and the reward/weight ordering are all pure Python, and keeping
them in the ordinary test job is what makes them run on every PR rather
than only in the heavier train-extra job. The handful of cases that need
`trl`/`datasets` say so with `importorskip`.

Episodes are hand-constructed here, in the style of the existing
`test_rewards_*.py` files, rather than loaded from a fixture: a reward test
whose input you cannot read in the same screen as its assertion is a test
nobody re-derives when it fails.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from fde_training import lambda_grader, train
from fde_training.rewards import DEFAULT_WEIGHTS
from fde_training.sft_config import MaskVerificationError, TrainingProfile


def _record(**overrides: Any) -> dict[str, Any]:
    record = {
        "messages": [
            {"role": "system", "content": "You are the FDE Engagement Agent.", "trainable": False},
            {"role": "user", "content": "What gates manager approval?", "trainable": False},
            {
                "role": "assistant",
                "content": None,
                "trainable": True,
                "tool_calls": [
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {"name": "kg_search", "arguments": '{"k":5}'},
                    }
                ],
            },
            {
                "role": "tool",
                "tool_call_id": "call_1",
                "name": "kg_search",
                "content": '{"results":[{"node_key":"ctl.discount_threshold_20"}]}',
                "trainable": False,
            },
            {
                "role": "assistant",
                "content": "Gated by [ctl.discount_threshold_20].",
                "trainable": True,
            },
        ],
        "assistant_mask": [False, False, True, False, True],
        "meta": {"session_id": "s1", "split": "train"},
    }
    record.update(overrides)
    return record


# ===========================================================================
# to_trl_conversational
# ===========================================================================
def test_to_trl_conversational_strips_trainable_key() -> None:
    rows = train.to_trl_conversational([_record()])
    assert len(rows) == 1
    assert set(rows[0]) == {"messages"}
    for message in rows[0]["messages"]:
        assert "trainable" not in message


def test_to_trl_conversational_preserves_tool_call_plumbing() -> None:
    messages = train.to_trl_conversational([_record()])[0]["messages"]
    assistant = messages[2]
    assert assistant["tool_calls"][0]["function"]["name"] == "kg_search"
    tool = messages[3]
    assert tool["tool_call_id"] == "call_1"
    assert tool["name"] == "kg_search"


def test_to_trl_conversational_drops_none_content_but_keeps_the_message() -> None:
    """A tool-call-only assistant turn has `content=None`; the chat template
    handles that itself, but a literal None key confuses `apply_chat_template`."""
    assistant = train.to_trl_conversational([_record()])[0]["messages"][2]
    assert "content" not in assistant
    assert assistant["role"] == "assistant"


def test_to_trl_conversational_rejects_record_without_messages() -> None:
    with pytest.raises(ValueError, match="no non-empty 'messages'"):
        train.to_trl_conversational([{"assistant_mask": [], "meta": {}}])


# ===========================================================================
# load_sft_dataset
# ===========================================================================
def test_load_sft_dataset_rejects_wrong_path_count() -> None:
    """Argument validation happens before the heavy import, so a wrong
    invocation reports the wrong invocation even with no `train` extra."""
    with pytest.raises(ValueError, match="one JSONL path, or exactly three"):
        train.load_sft_dataset(["a.jsonl", "b.jsonl"])


def _write_jsonl(path: Any, records: list[dict[str, Any]]) -> str:
    path.write_text("".join(json.dumps(r) + "\n" for r in records))
    return str(path)


def test_load_sft_dataset_single_file_splits_by_meta(tmp_path: Any) -> None:
    pytest.importorskip("datasets")
    records = [
        _record(meta={"session_id": "a", "split": "train"}),
        _record(meta={"session_id": "b", "split": "validation"}),
        _record(meta={"session_id": "c", "split": "test"}),
    ]
    path = _write_jsonl(tmp_path / "all.jsonl", records)
    dataset = train.load_sft_dataset([path])
    assert set(dataset) == {"train", "validation", "test"}
    assert len(dataset["train"]) == 1


def test_load_sft_dataset_three_files_map_positionally(tmp_path: Any) -> None:
    pytest.importorskip("datasets")
    paths = [
        _write_jsonl(tmp_path / "train.jsonl", [_record(), _record()]),
        _write_jsonl(tmp_path / "val.jsonl", [_record()]),
        _write_jsonl(tmp_path / "test.jsonl", [_record()]),
    ]
    dataset = train.load_sft_dataset(paths)
    assert len(dataset["train"]) == 2
    assert len(dataset["validation"]) == 1


def test_load_sft_dataset_refuses_empty_train_split(tmp_path: Any) -> None:
    pytest.importorskip("datasets")
    path = _write_jsonl(tmp_path / "all.jsonl", [_record(meta={"split": "test"})])
    with pytest.raises(ValueError, match="train split is empty"):
        train.load_sft_dataset([path])


def test_run_sft_aborts_before_training_when_the_mask_is_wrong(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The masking check must run BEFORE the trainer is constructed --
    catching it after the GPU is warm is catching it too late."""
    pytest.importorskip("trl")
    constructed: list[str] = []

    def _boom(*args: Any, **kwargs: Any) -> None:
        raise MaskVerificationError("token mismatch")

    class _Tokenizer:
        is_fast = True

    def _trainer(*args: Any, **kwargs: Any) -> None:
        constructed.append("SFTTrainer")

    monkeypatch.setattr(train, "verify_masking", _boom)
    monkeypatch.setattr(train, "load_tokenizer", lambda model_id: _Tokenizer())
    monkeypatch.setattr("trl.SFTTrainer", _trainer)

    spec = train.SFTRunSpec(profile=TrainingProfile(model_id="stub", output_dir=str(tmp_path)))
    with pytest.raises(MaskVerificationError):
        train.run_sft(spec, {"train": [{"messages": _record()["messages"]}]})
    assert constructed == []


# ===========================================================================
# QA reward adapters
# ===========================================================================
def test_r_grounded_qa_scores_fully_grounded_partial_and_abstention() -> None:
    scores = train.r_grounded_qa(
        completions=[
            "Approval is gated by [ctl.discount_threshold_20].",
            "It involves [ctl.discount_threshold_20] and [made.up].",
            "No information found for this question.",
        ],
        provenance_keys=[
            ["ctl.discount_threshold_20", "act.create_quote"],
            ["ctl.discount_threshold_20"],
            ["ctl.discount_threshold_20"],
        ],
    )
    assert scores == [1.0, 0.5, 1.0]


def test_r_outcome_qa_is_f1_against_gold_and_rewards_both_empty() -> None:
    scores = train.r_outcome_qa(
        completions=["[a.one] and [a.two]", "[a.one]", "nothing cited here"],
        gold_keys=[["a.one", "a.two"], ["a.one", "a.two"], []],
    )
    assert scores[0] == 1.0
    assert scores[1] == pytest.approx(2 / 3)
    assert scores[2] == 1.0


def test_r_citation_qa_counts_substantive_sentences() -> None:
    assert train.r_citation_qa(completions=["A sentence with no citation at all."]) == [0.0]
    assert train.r_citation_qa(completions=["Gated by [ctl.x] as documented here."]) == [1.0]
    assert train.r_citation_qa(completions=[""]) == [1.0]


def test_qa_reward_funcs_order_matches_lambda_grader_weights() -> None:
    """The managed grader and local GRPO must weight the same terms the same
    way, or the two RL paths optimise different objectives."""
    funcs, weights = train.qa_reward_funcs()
    assert [f.__name__ for f in funcs] == ["r_grounded_qa", "r_outcome_qa", "r_citation_qa"]
    assert weights == [lambda_grader.WEIGHTS[k] for k in ("grounded", "outcome", "citation")]
    assert sum(weights) == pytest.approx(1.0)


@pytest.mark.parametrize(
    ("completion", "expected"),
    [
        ("plain text", "plain text"),
        ([{"role": "assistant", "content": "from a message list"}], "from a message list"),
        ({"role": "assistant", "content": "from a dict"}, "from a dict"),
    ],
)
def test_completion_text_handles_every_trl_completion_shape(completion: Any, expected: str) -> None:
    assert train._completion_text(completion) == expected


# ===========================================================================
# GRPO config + dataset
# ===========================================================================
def test_build_qa_grpo_dataset_lifts_reference_response_to_columns(tmp_path: Any) -> None:
    pytest.importorskip("datasets")
    path = tmp_path / "rft.jsonl"
    path.write_text(
        json.dumps(
            {
                "prompt": "Who approves a deep discount?",
                "referenceResponse": {
                    "gold_keys": ["role.deal_desk"],
                    "provenance_keys": ["role.deal_desk", "act.discount_review"],
                },
            }
        )
        + "\n"
    )
    dataset = train.build_qa_grpo_dataset(path=str(path))
    assert dataset.column_names == ["prompt", "gold_keys", "provenance_keys"]
    assert dataset[0]["gold_keys"] == ["role.deal_desk"]
    assert len(dataset[0]["provenance_keys"]) == 2


def test_build_grpo_config_maps_profile_fields_and_reward_weights() -> None:
    # bf16=False, unlike the profile default: GRPOConfig is a TrainingArguments
    # subclass that validates bf16 against the ACTUAL hardware at construction,
    # and CI runners are CPU-only. The mapping is what's under test, not the
    # accelerator; bf16 pass-through is asserted explicitly below.
    pytest.importorskip("trl")
    profile = train.GRPOProfile(
        output_dir="out/grpo", num_generations=4, beta=0.02, learning_rate=3e-6, seed=7, bf16=False
    )
    config = train.build_grpo_config(profile, [0.45, 0.4, 0.15])
    assert config.num_generations == 4
    assert config.beta == pytest.approx(0.02)
    assert config.learning_rate == pytest.approx(3e-6)
    assert config.seed == 7
    assert config.reward_weights == [0.45, 0.4, 0.15]
    assert config.bf16 is False


def test_episode_reward_funcs_track_default_weights_exactly() -> None:
    funcs, weights = train.episode_reward_funcs()
    assert [f.__name__ for f in funcs] == list(DEFAULT_WEIGHTS)
    assert weights == list(DEFAULT_WEIGHTS.values())
    assert sum(weights) == pytest.approx(1.0)


# ===========================================================================
# GrpoRolloutEnvironment + env_composite_reward
# ===========================================================================
class _StubEnv:
    """Stand-in for `rollout_env.RolloutEnv` recording what was delegated."""

    def __init__(self, engagement_id: str, tool_budget: int = 8) -> None:
        self.engagement_id = engagement_id
        self.tool_budget = tool_budget
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.reset_args: dict[str, Any] | None = None

    def reset(self, task_kind: str, task_input: dict[str, Any]) -> dict[str, Any]:
        self.reset_args = {"task_kind": task_kind, "task_input": task_input}
        return {"session_id": "stub"}

    def kg_search(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("kg_search", kwargs))
        return {"results": [{"node_key": "ctl.discount_threshold_20"}]}

    def kg_traverse(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("kg_traverse", kwargs))
        return {"nodes": []}

    def kg_get_node(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("kg_get_node", kwargs))
        return {"node": None}

    def kg_propose(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("kg_propose", kwargs))
        return {"valid": True, "errors": []}

    def transcript(self) -> list[dict[str, Any]]:
        return [
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {
                            "name": "kg_search",
                            "arguments": '{"k":5,"query":"who approves"}',
                        },
                    }
                ],
            },
            {
                "role": "tool",
                "tool_call_id": "call_1",
                "name": "kg_search",
                "content": '{"results":[{"node_key":"ctl.discount_threshold_20"}]}',
            },
            {"role": "assistant", "content": "Gated by [ctl.discount_threshold_20]."},
        ]


def _stub_environment(**kwargs: Any) -> train.GrpoRolloutEnvironment:
    env = train.GrpoRolloutEnvironment(
        engagement_id="eng-1", tool_budget=5, env_factory=_StubEnv, **kwargs
    )
    env.reset(question="who approves a deep discount?")
    return env


def test_grpo_environment_reset_creates_a_grpo_rollout_episode() -> None:
    env = _stub_environment()
    assert env._env.reset_args == {
        "task_kind": "grpo_rollout",
        "task_input": {"question": "who approves a deep discount?"},
    }
    assert env._env.tool_budget == 5


def test_grpo_environment_delegates_every_tool_to_the_rollout_env() -> None:
    env = _stub_environment()
    env.kg_search(query="q", k=3, expand_hops=1)
    env.kg_traverse(start_keys=["a"], max_hops=2, direction="in")
    env.kg_get_node(node_key="a")
    env.kg_propose(title="t", rationale="r", items=[])
    assert [name for name, _ in env._env.calls] == [
        "kg_search",
        "kg_traverse",
        "kg_get_node",
        "kg_propose",
    ]
    assert env._env.calls[0][1] == {"query": "q", "k": 3, "expand_hops": 1}


def test_grpo_environment_tool_before_reset_is_an_error_not_a_silent_no_op() -> None:
    env = train.GrpoRolloutEnvironment(engagement_id="eng-1", env_factory=_StubEnv)
    with pytest.raises(RuntimeError, match="reset\\(\\) must be called"):
        env.kg_search(query="q")


def test_grpo_environment_exposes_exactly_the_four_tools_as_public_methods() -> None:
    """TRL turns every public method of an environment into a tool the policy
    may call. A transcript accessor or a helper that leaks into that list
    burns tool budget on something the model cannot use, so the public
    surface is asserted rather than assumed."""
    public = {
        name
        for name in dir(train.GrpoRolloutEnvironment)
        if not name.startswith("_") and callable(getattr(train.GrpoRolloutEnvironment, name))
    }
    assert public == {"reset", "kg_search", "kg_traverse", "kg_get_node", "kg_propose"}


def test_env_composite_reward_scores_each_environments_own_transcript() -> None:
    envs = [_stub_environment(), _stub_environment()]
    rewards = train.env_composite_reward(
        completions=["ignored", "ignored"],
        environments=envs,
        gold_keys=[["ctl.discount_threshold_20"], ["ctl.discount_threshold_20"]],
    )
    assert len(rewards) == 2
    assert all(0.0 < r <= 1.0 for r in rewards)
    assert rewards[0] == pytest.approx(rewards[1])


def test_env_composite_reward_ignores_trl_bookkeeping_kwargs() -> None:
    """TRL passes `trainer_state`/`log_metric`/`completion_ids` to every
    reward function; forwarding them into `compose_rewards` would blow up
    the reward terms that do not accept them."""
    rewards = train.env_composite_reward(
        completions=["x"],
        environments=[_stub_environment()],
        trainer_state=object(),
        log_metric=lambda *a, **k: None,
        log_extra=lambda *a, **k: None,
        completion_ids=[[1, 2, 3]],
        prompts=["p"],
    )
    assert len(rewards) == 1


def test_env_composite_reward_without_environments_returns_zeros() -> None:
    assert train.env_composite_reward(completions=["a", "b"], environments=None) == [0.0, 0.0]


def test_env_composite_reward_scores_an_episode_that_did_nothing_as_zero() -> None:
    """Every composite term is vacuously satisfied by an empty transcript, so
    a rollout that never called a tool would otherwise score ~0.85 -- i.e.
    the policy would be paid to stop retrieving. It scores 0.0, and a
    broken episode alongside good ones does not take the batch down."""
    broken = train.GrpoRolloutEnvironment(engagement_id="eng-1", env_factory=_StubEnv)
    rewards = train.env_composite_reward(
        completions=["a", "b"],
        environments=[broken, _stub_environment()],
        gold_keys=[["ctl.discount_threshold_20"], ["ctl.discount_threshold_20"]],
    )
    assert rewards[0] == 0.0
    assert rewards[1] > 0.5
