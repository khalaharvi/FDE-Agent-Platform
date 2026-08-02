"""train.py -- the SFT and GRPO trainers `sft_config.py` and
`fde_training.rewards` were always building toward.

Before this module, `build_sft_config()` and `build_lora_config()` had zero
callers and `DEFAULT_WEIGHTS` was never handed to a trainer: the pipeline
could export a dataset, verify its loss mask, and score a reward, but not
train anything. This module is the missing consumer, and nothing more --
every piece of policy (what the mask is, what the reward is, what the LoRA
target modules are) still lives where it lived.

Heavy-import policy
--------------------
Same contract as `sft_config.py`, and for the same reason: `torch`, `trl`,
`peft`, `transformers`, and `datasets` are imported INSIDE the functions
that need them. `import fde_training.train` works on a laptop with no GPU
stack, which is what lets the pure parts below (`to_trl_conversational`,
`qa_reward_funcs`, `GrpoRolloutEnvironment`, `env_composite_reward`) be
unit-tested in the ordinary test job rather than only in the train-extra
one. `_require_train_extra` turns a missing dependency into the install
command instead of a bare ImportError.

Two GRPO modes, and an honest account of what each can teach
--------------------------------------------------------------
`mode="qa"` is the supported, tested path: single-turn retrieval QA over
`trn.eval_query` prompts, where grounding and outcome are computed from the
completion text against the `provenance_keys`/`gold_keys` columns the
dataset carries. It trains citation discipline and answer correctness. It
CANNOT teach when to stop searching, because there is no tool loop in it --
the retrieval already happened, at dataset-build time.

`mode="env"` wires TRL's `environment_factory` to `GrpoRolloutEnvironment`,
which puts the real `RolloutEnv` (real Postgres, real `kg.hybrid_search`)
behind the model's tool calls, and scores with `env_composite_reward` over
the environment's OWN transcript rather than over whatever shape the
trainer hands back. That last choice is the load-bearing one: the reward
then measures what the episode actually did against the database, so it
stays correct even if TRL's completion shape changes underneath it. This
mode is EXPERIMENTAL. The adapter and the reward closure are unit-tested
with stub environments; the end-to-end trainer loop needs a GPU and a real
model and has not been run here. TRL itself marks `environment_factory`
experimental and warns on use. For serious rollout throughput,
`docs/06-training.md` §5's frameworks table still points at verl, and
`rollout_env.VerlRolloutTool` is the adapter for it.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from fde_mcp.logging import get_logger
from fde_training import lambda_grader
from fde_training.rewards import DEFAULT_WEIGHTS, REWARD_FUNCTIONS, compose_rewards
from fde_training.sft_config import (
    DEFAULT_LORA_ALPHA,
    DEFAULT_LORA_DROPOUT,
    DEFAULT_LORA_R,
    TrainingProfile,
    build_lora_config,
    build_sft_config,
    verify_masking,
)

if TYPE_CHECKING:
    from datasets import Dataset, DatasetDict
    from trl import GRPOConfig

log = get_logger(__name__)

# Message keys the chat template reads. `trainable` is export_sft.py's own
# bookkeeping and is NOT one of them -- `apply_chat_template` raises on
# unknown keys, so it must be stripped before a record reaches the trainer.
_CHAT_MESSAGE_KEYS = ("role", "content", "tool_calls", "tool_call_id", "name")

_INSTALL_HINT = "install the train extra: uv sync --package fde-training --extra train"


def _require_train_extra(module_name: str) -> None:
    """Raise a message an operator can act on when a heavy dependency is
    absent, instead of a bare ImportError from three frames down."""
    import importlib.util  # noqa: PLC0415

    if importlib.util.find_spec(module_name) is None:
        msg = f"{module_name} is not installed -- {_INSTALL_HINT}"
        raise RuntimeError(msg)


# ===========================================================================
# SFT
# ===========================================================================
@dataclass
class SFTRunSpec:
    """One SFT run's full configuration.

    Attributes:
        profile: The `TrainingProfile` (model, output dir, batch/epoch/seq
            settings) `build_sft_config` turns into an `SFTConfig`.
        lora_r: LoRA rank; ignored when `profile.use_lora` is False.
        lora_alpha: LoRA alpha.
        lora_dropout: LoRA dropout.
    """

    profile: TrainingProfile
    lora_r: int = DEFAULT_LORA_R
    lora_alpha: int = DEFAULT_LORA_ALPHA
    lora_dropout: float = DEFAULT_LORA_DROPOUT


def to_trl_conversational(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Convert `export_sft.py` records into TRL's conversational rows.

    Pure: no heavy imports, so this is unit-testable anywhere. Drops every
    message key the chat template does not read (notably `trainable`) and
    keeps `tool_calls`/`tool_call_id`/`name` intact, because a multi-turn
    tool trace that loses them renders as a plain chat and silently trains
    the model on a different task than the one it was traced doing.

    Args:
        records: `{"messages": [...], "assistant_mask": [...], "meta": {...}}`.

    Returns:
        `[{"messages": [...]}, ...]`.

    Raises:
        ValueError: if a record has no `messages` list.
    """
    out = []
    for i, record in enumerate(records):
        messages = record.get("messages")
        if not isinstance(messages, list) or not messages:
            msg = f"record {i} has no non-empty 'messages' list"
            raise ValueError(msg)
        out.append(
            {
                "messages": [
                    {k: m[k] for k in _CHAT_MESSAGE_KEYS if k in m and m[k] is not None}
                    for m in messages
                ]
            }
        )
    return out


def _read_jsonl(path: str | Path) -> list[dict[str, Any]]:
    with Path(path).open() as f:
        return [json.loads(line) for line in f if line.strip()]


def load_sft_dataset(paths: list[str] | None, *, from_db: bool = False) -> DatasetDict:
    """Build the train/validation/test `DatasetDict` an SFT run consumes.

    Three sources, in the order the CLI offers them:

    * one JSONL path -- split by each record's `meta.split`, which is what
      `export_sft.py` writes when handed a single `--out`;
    * exactly three JSONL paths -- `[train, validation, test]`, matching
      `export-sft --out a b c`;
    * `from_db=True` -- run the export in-process, so a training job does
      not need an intermediate file at all.

    Args:
        paths: JSONL path(s), or None with `from_db=True`.
        from_db: Export from Postgres instead of reading files.

    Returns:
        A `DatasetDict` with a `train` split and, when non-empty,
        `validation`/`test`.

    Raises:
        ValueError: on an unsupported number of paths, or an empty train
            split (training on nothing is a failure, not a no-op).
    """
    if not from_db and (paths is None or len(paths) not in (1, 3)):
        # Checked before the heavy import so a wrong invocation reports the
        # wrong invocation, not a missing dependency.
        msg = "load_sft_dataset needs --from-db, one JSONL path, or exactly three [train validation test]"
        raise ValueError(msg)

    _require_train_extra("datasets")
    from datasets import Dataset, DatasetDict  # noqa: PLC0415

    by_split: dict[str, list[dict[str, Any]]] = {"train": [], "validation": [], "test": []}
    if from_db:
        from fde_training import export_sft  # noqa: PLC0415 -- DB path only

        records, stats = export_sft.build_records(export_sft.fetch_rows())
        log.info("sft_dataset_exported", **stats)
        for record in records:
            by_split.setdefault(record["meta"]["split"], []).append(record)
    elif paths and len(paths) == 1:
        for record in _read_jsonl(paths[0]):
            by_split.setdefault(record.get("meta", {}).get("split", "train"), []).append(record)
    elif paths:
        for path, split in zip(paths, ("train", "validation", "test"), strict=True):
            by_split[split] = _read_jsonl(path)

    if not by_split.get("train"):
        msg = "the train split is empty -- nothing to train on"
        raise ValueError(msg)

    splits = {
        name: Dataset.from_list(to_trl_conversational(rows))
        for name, rows in by_split.items()
        if rows
    }
    log.info("sft_dataset_loaded", **{name: len(ds) for name, ds in splits.items()})
    return DatasetDict(splits)


def load_tokenizer(model_id: str) -> Any:
    """Resolve a run's tokenizer.

    A named function rather than an inline `AutoTokenizer.from_pretrained`
    so there is one seam a test can replace. Patching the call inside
    `run_sft` is not possible -- the import is function-local by the
    heavy-import policy -- and without a seam every `run_sft` test would
    need a real model repo and a network round trip to reach the assertion
    it actually cares about.

    Args:
        model_id: HF model id or local checkpoint path.

    Returns:
        The tokenizer, which `verify_masking` requires to be a fast one.
    """
    from transformers import AutoTokenizer  # noqa: PLC0415

    return AutoTokenizer.from_pretrained(model_id)


def run_sft(spec: SFTRunSpec, dataset: DatasetDict, *, verify: bool = True) -> str:
    """Run supervised fine-tuning and return the output directory.

    `verify=True` (the default) runs `verify_masking` over a sample of the
    train split FIRST, using the run's own tokenizer, and lets the
    `MaskVerificationError` propagate. That ordering is the point: a loss
    mask that disagrees with the export is the most expensive failure in
    this pipeline (see `chat_template.jinja`'s docstring), and it is only
    cheap to catch before the GPU hours, not after.

    Args:
        spec: The run configuration.
        dataset: Output of `load_sft_dataset`.
        verify: Run the masking tripwire first.

    Returns:
        `spec.profile.output_dir`.
    """
    _require_train_extra("trl")
    from trl import SFTTrainer  # noqa: PLC0415

    tokenizer = load_tokenizer(spec.profile.model_id)
    if verify:
        summary = verify_masking(_verification_records(dataset["train"]), tokenizer)
        log.info(
            "sft_masking_verified",
            **{k: summary[k] for k in ("examples_checked", "tokens_checked")},
        )

    args = build_sft_config(spec.profile)
    peft_config = (
        build_lora_config(spec.lora_r, spec.lora_alpha, spec.lora_dropout)
        if spec.profile.use_lora
        else None
    )
    trainer = SFTTrainer(
        model=spec.profile.model_id,
        args=args,
        train_dataset=dataset["train"],
        eval_dataset=dataset.get("validation"),
        processing_class=tokenizer,
        peft_config=peft_config,
    )
    log.info(
        "sft_training_start",
        model_id=spec.profile.model_id,
        use_lora=spec.profile.use_lora,
        n_train=len(dataset["train"]),
    )
    trainer.train()
    trainer.save_model(spec.profile.output_dir)
    log.info("sft_training_done", output_dir=spec.profile.output_dir)
    return spec.profile.output_dir


def _verification_records(train_split: Any, n: int = 8) -> list[dict[str, Any]]:
    """Rebuild the `{"messages", "assistant_mask"}` shape `verify_masking`
    reads from the conversational rows the trainer will actually see.

    The mask is re-derived from `role == "assistant"`, which is the same
    rule `export_sft.py` applies -- so this checks the template against the
    role structure that survived conversion, catching a conversion bug as
    well as a template one.
    """
    records = []
    for row in list(train_split)[:n]:
        messages = row["messages"]
        records.append(
            {
                "messages": messages,
                "assistant_mask": [m["role"] == "assistant" for m in messages],
                "meta": {"session_id": "sft-preflight"},
            }
        )
    return records


# ===========================================================================
# GRPO
# ===========================================================================
@dataclass
class GRPOProfile:
    """One GRPO run's configuration; the direct analogue of
    `TrainingProfile` for the RL stage.

    Attributes:
        model_id: Policy model to start from (an SFT checkpoint, normally).
        output_dir: Where checkpoints land.
        num_generations: Completions sampled per prompt -- GRPO's group
            size, and what the advantage is computed within.
        max_completion_length: Token ceiling per completion.
        per_device_train_batch_size: Prompts per device per step.
        gradient_accumulation_steps: Steps accumulated before an update.
        learning_rate: 1e-5 -- an order of magnitude below the SFT LoRA rate;
            RL updates a policy that is already competent and a large step
            destroys it faster than it improves it.
        beta: KL coefficient against the reference policy. 0.0 by default
            per `docs/06-training.md` §5: with a composite reward that
            already contains format/schema gates, the KL term mostly slows
            learning without preventing the collapse it is nominally there
            to prevent -- `RewardHackingMonitor` is the guard that actually
            watches for that.
        bf16: bfloat16 training.
        seed: RNG seed.
        use_lora: Train LoRA adapters rather than full weights.
    """

    model_id: str = "Qwen/Qwen2.5-7B-Instruct"
    output_dir: str = "./grpo-out"
    num_generations: int = 8
    max_completion_length: int = 512
    per_device_train_batch_size: int = 2
    gradient_accumulation_steps: int = 8
    learning_rate: float = 1e-5
    beta: float = 0.0
    bf16: bool = True
    seed: int = 0
    use_lora: bool = True


def build_grpo_config(profile: GRPOProfile, reward_weights: list[float]) -> GRPOConfig:
    """Map a `GRPOProfile` onto TRL's `GRPOConfig`.

    Only long-stable `GRPOConfig` fields are set. Version-fragile knobs
    (vLLM colocation flags, generation backends) are deliberately absent:
    they change between TRL minors, and a config module that sets them
    turns a dependency bump into a broken training run rather than a
    deprecation warning. An operator who needs them can layer them on.

    Args:
        profile: The run configuration.
        reward_weights: One weight per reward function, in the SAME ORDER
            the functions are passed to `GRPOTrainer`. TRL does not check
            this correspondence for you.

    Returns:
        A `GRPOConfig`.
    """
    _require_train_extra("trl")
    from trl import GRPOConfig  # noqa: PLC0415

    return GRPOConfig(
        output_dir=profile.output_dir,
        num_generations=profile.num_generations,
        max_completion_length=profile.max_completion_length,
        per_device_train_batch_size=profile.per_device_train_batch_size,
        gradient_accumulation_steps=profile.gradient_accumulation_steps,
        learning_rate=profile.learning_rate,
        lr_scheduler_type="cosine",
        warmup_ratio=0.03,
        beta=profile.beta,
        reward_weights=reward_weights,
        bf16=profile.bf16,
        seed=profile.seed,
        logging_steps=5,
        save_strategy="steps",
        save_steps=50,
        save_total_limit=3,
        report_to=["none"],
    )


def build_qa_grpo_dataset(
    engagement_id: str | None = None,
    k: int = 10,
    embed_fn: Callable[[str], list[float]] | None = None,
    path: str | None = None,
) -> Dataset:
    """The single-turn QA prompt dataset, flattened for TRL.

    ONE dataset builder is shared by Bedrock RFT and local GRPO:
    `bedrock_rft.build_dataset` produces `{"prompt", "referenceResponse":
    {"gold_keys", "provenance_keys"}}`, and this function lifts the nested
    reference fields to top-level columns, which is the shape TRL forwards
    to reward functions as per-example kwargs. Keeping one builder is what
    stops the managed and self-managed RL paths from silently optimising
    against two different gold sets.

    Args:
        engagement_id: Restrict to one engagement; None means every query.
        k: Retrieval depth used to build `provenance_keys`.
        embed_fn: Embedding function; None means the real Bedrock path
            (see `bedrock_rft.build_dataset`).
        path: Read a JSONL previously written by `rft-submit build-dataset`
            instead of querying Postgres. Mutually exclusive with the DB
            arguments in practice, and preferred for reproducible reruns.

    Returns:
        A `Dataset` with `prompt`, `gold_keys`, and `provenance_keys`.
    """
    _require_train_extra("datasets")
    from datasets import Dataset  # noqa: PLC0415

    if path is not None:
        rows = _read_jsonl(path)
    else:
        from fde_training import bedrock_rft  # noqa: PLC0415 -- DB path only

        rows = bedrock_rft.build_dataset(engagement_id=engagement_id, k=k, embed_fn=embed_fn)

    flattened = [
        {
            "prompt": row["prompt"],
            "gold_keys": list(row.get("referenceResponse", {}).get("gold_keys", [])),
            "provenance_keys": list(row.get("referenceResponse", {}).get("provenance_keys", [])),
        }
        for row in rows
    ]
    log.info("grpo_qa_dataset_built", n_prompts=len(flattened), from_path=path)
    return Dataset.from_list(flattened)


def _completion_text(completion: Any) -> str:
    """Coerce whatever TRL hands a reward function into answer text.

    Standard (prompt-only) datasets give a string; conversational ones give
    a message list. Both shapes reach these reward functions depending on
    how the operator built the dataset, so both are handled here rather
    than in three near-identical places below.
    """
    if isinstance(completion, str):
        return completion
    if isinstance(completion, list):
        return "\n".join(str(m.get("content") or "") for m in completion if isinstance(m, dict))
    if isinstance(completion, dict):
        return str(completion.get("content") or "")
    return str(completion)


def r_grounded_qa(
    completions: list[Any] | None = None,
    provenance_keys: list[list[str]] | None = None,
    **kwargs: Any,
) -> list[float]:
    """Fraction of the answer's citations that appear in the retrieval this
    prompt was built from; abstention scores 1.0.

    A thin adapter over `lambda_grader._grounded_score`, so the Bedrock RFT
    grader and local GRPO cannot drift apart on what "grounded" means.
    """
    del kwargs
    texts = [_completion_text(c) for c in completions or []]
    provenance = provenance_keys or [[] for _ in texts]
    return [
        lambda_grader._grounded_score(text, list(keys))
        for text, keys in zip(texts, provenance, strict=True)
    ]


def r_outcome_qa(
    completions: list[Any] | None = None,
    gold_keys: list[list[str]] | None = None,
    **kwargs: Any,
) -> list[float]:
    """F1 of cited keys against the query's gold key set
    (`lambda_grader._outcome_f1`)."""
    del kwargs
    texts = [_completion_text(c) for c in completions or []]
    gold = gold_keys or [[] for _ in texts]
    return [
        lambda_grader._outcome_f1(text, list(keys)) for text, keys in zip(texts, gold, strict=True)
    ]


def r_citation_qa(completions: list[Any] | None = None, **kwargs: Any) -> list[float]:
    """Fraction of substantive sentences carrying at least one citation
    (`lambda_grader._citation_format_score`)."""
    del kwargs
    return [lambda_grader._citation_format_score(_completion_text(c)) for c in completions or []]


def qa_reward_funcs() -> tuple[list[Callable[..., list[float]]], list[float]]:
    """The QA-mode reward functions and their weights, in matching order.

    Weights come from `lambda_grader.WEIGHTS`, read at call time rather
    than copied, so changing the managed grader's weighting changes this
    too. Named module-level functions (not lambdas) because TRL logs each
    reward under its function name -- `rewards/r_grounded_qa` in the run
    log is worth more than `rewards/<lambda>`.

    Returns:
        `(functions, weights)`.
    """
    funcs: dict[str, Callable[..., list[float]]] = {
        "grounded": r_grounded_qa,
        "outcome": r_outcome_qa,
        "citation": r_citation_qa,
    }
    ordered = list(lambda_grader.WEIGHTS)
    return [funcs[name] for name in ordered], [lambda_grader.WEIGHTS[name] for name in ordered]


def episode_reward_funcs() -> tuple[list[Callable[..., list[float]]], list[float]]:
    """The eight episode-level reward terms and `DEFAULT_WEIGHTS`, in the
    same iteration order.

    For a trainer whose completions ARE full tool transcripts. In `env`
    mode prefer `env_composite_reward`, which reads the environment's own
    record of the episode instead of trusting the completion shape.

    Returns:
        `(functions, weights)` -- the weights sum to 1.0 by construction of
        `DEFAULT_WEIGHTS`.
    """
    names = list(DEFAULT_WEIGHTS)
    return [REWARD_FUNCTIONS[name] for name in names], [DEFAULT_WEIGHTS[name] for name in names]


class GrpoRolloutEnvironment:
    """TRL `environment_factory` adapter over `rollout_env.RolloutEnv`.

    TRL turns every PUBLIC method of an environment instance into a tool
    the policy may call, and requires a `reset` it can call between
    generations. That rule is why the transcript accessor below is
    `_transcript` and not `transcript`: a public one would be advertised to
    the model as a callable tool, which is both useless to it and a way to
    burn tool budget.

    One instance per rollout, which is exactly what `RolloutEnv`'s
    per-episode `session_id` threading was built for -- see that module's
    docstring on why concurrent rollouts cannot share process-global
    session state.
    """

    def __init__(
        self,
        engagement_id: str,
        tool_budget: int = 8,
        embed_fn: Callable[[str], list[float]] | None = None,
        env_factory: Callable[..., Any] | None = None,
    ) -> None:
        """
        Args:
            engagement_id: The graph this rollout reasons over.
            tool_budget: Per-episode tool-call ceiling.
            embed_fn: Embedding function for `kg_search`; None uses
                `RolloutEnv`'s own default (the real Bedrock path).
            env_factory: Constructor for the underlying environment.
                Injected by tests with a stub; production leaves it None.
        """
        self.engagement_id = engagement_id
        self.tool_budget = tool_budget
        self.embed_fn = embed_fn
        self._env_factory = env_factory
        self._env: Any = None

    def reset(self, **kwargs: Any) -> None:
        """Start a fresh episode. Returns None (TRL appends a returned
        string to the last user message; we have nothing to add -- the
        prompt already carries the question)."""
        if self._env_factory is not None:
            self._env = self._env_factory(
                engagement_id=self.engagement_id, tool_budget=self.tool_budget
            )
        else:
            from fde_training import rollout_env  # noqa: PLC0415

            env_kwargs: dict[str, Any] = {"tool_budget": self.tool_budget}
            if self.embed_fn is not None:
                env_kwargs["embed_fn"] = self.embed_fn
            self._env = rollout_env.RolloutEnv(engagement_id=self.engagement_id, **env_kwargs)
        self._env.reset(task_kind="grpo_rollout", task_input=dict(kwargs))

    def _require_env(self) -> Any:
        if self._env is None:
            msg = "reset() must be called before any tool method"
            raise RuntimeError(msg)
        return self._env

    def _transcript(self) -> list[dict[str, Any]]:
        """The episode so far in `EpisodeLog` message shape. Underscored so
        TRL does not expose it as a tool (see the class docstring)."""
        return self._require_env().transcript()  # type: ignore[no-any-return]

    def kg_search(self, query: str, k: int = 20, expand_hops: int = 2) -> dict[str, Any]:
        """Hybrid ANN + graph retrieval over the knowledge graph.

        Args:
            query: Natural-language question to retrieve evidence for.
            k: Maximum number of results to return.
            expand_hops: Graph-expansion depth from the top ANN seeds.

        Returns:
            A dict with `results`: a list of {node_key, node_type, label,
            summary, rrf_score, provenance} ranked by relevance.
        """
        return self._require_env().kg_search(query=query, k=k, expand_hops=expand_hops)  # type: ignore[no-any-return]

    def kg_traverse(
        self, start_keys: list[str], max_hops: int = 3, direction: str = "out"
    ) -> dict[str, Any]:
        """Bounded multi-hop traversal from one or more start nodes.

        Args:
            start_keys: node_key values to start from.
            max_hops: Depth ceiling (<=6).
            direction: 'out', 'in', or 'both'.

        Returns:
            A dict with `nodes`: reachable nodes with depth/path/confidence.
        """
        return self._require_env().kg_traverse(  # type: ignore[no-any-return]
            start_keys=start_keys, max_hops=max_hops, direction=direction
        )

    def kg_get_node(self, node_key: str) -> dict[str, Any]:
        """Fetch one live node.

        Args:
            node_key: The node's stable key.

        Returns:
            A dict with `node`: the node row, or {"node": None} if absent.
        """
        return self._require_env().kg_get_node(node_key=node_key)  # type: ignore[no-any-return]

    def kg_propose(self, title: str, rationale: str, items: list[dict[str, Any]]) -> dict[str, Any]:
        """Propose a graph change (terminal action; never actually written).

        Args:
            title: Short proposal title.
            rationale: Why this change is warranted.
            items: Proposal items shaped like hitl.proposal_item
                (op/node_type|edge_type/subject_key/payload/source_ids/
                agent_confidence).

        Returns:
            A dict with `valid`, `errors`, and the echoed proposal.
        """
        return self._require_env().kg_propose(  # type: ignore[no-any-return]
            title=title, rationale=rationale, items=items
        )


def env_composite_reward(
    completions: list[Any] | None = None,
    environments: list[Any] | None = None,
    **kwargs: Any,
) -> list[float]:
    """Score each rollout from its ENVIRONMENT's transcript, not the
    trainer's completion.

    TRL exposes the per-completion environment instances to reward
    functions as an `environments` kwarg. Reading the environment's own
    record is the honest bridge: it is what the episode actually did
    against Postgres, so `compose_rewards` sees the same transcript
    `generate_traces.py` and the SFT export see, and the reward stays
    correct even if the trainer's completion representation changes.

    An empty transcript scores 0.0, and does NOT go through
    `compose_rewards`. This matters more than it looks: every term in the
    composite is vacuously satisfied by an episode that did nothing (no
    malformed tool call, nothing uncited, no wasted hops), so an episode
    with no tool calls scores ~0.85 if you let it through -- which trains
    the policy to answer without retrieving, the exact collapse
    `RewardHackingMonitor` exists to detect. In `env` mode a rollout that
    never touched the graph is a non-episode, and it is scored as one.

    Args:
        completions: TRL's completions; used only for length when
            `environments` is absent.
        environments: One environment instance per completion.
        **kwargs: Other TRL-supplied kwargs. Per-example columns (e.g.
            `gold_keys`) are forwarded to `compose_rewards`, realigned to
            the scored subset; TRL's own bookkeeping kwargs are dropped.

    Returns:
        One combined scalar per completion.
    """
    for key in ("trainer_state", "log_extra", "log_metric", "completion_ids", "prompts"):
        kwargs.pop(key, None)

    n = len(environments) if environments is not None else len(completions or [])
    if not environments:
        log.warning("env_composite_reward_no_environments", n_completions=n)
        return [0.0] * n

    transcripts: list[list[dict[str, Any]]] = []
    for env in environments:
        try:
            transcripts.append(env._transcript())
        except (RuntimeError, AttributeError):
            transcripts.append([])

    scored = [i for i, t in enumerate(transcripts) if t]
    rewards = [0.0] * n
    if not scored:
        log.warning("env_composite_reward_all_transcripts_empty", n_completions=n)
        return rewards

    sub_kwargs = {
        key: ([value[i] for i in scored] if isinstance(value, list) and len(value) == n else value)
        for key, value in kwargs.items()
    }
    combined, per_term = compose_rewards([transcripts[i] for i in scored], **sub_kwargs)
    for position, index in enumerate(scored):
        rewards[index] = combined[position]

    log.info(
        "env_composite_reward",
        n=n,
        n_without_transcript=n - len(scored),
        mean={name: sum(v) / len(v) for name, v in per_term.items() if v},
    )
    return rewards


def run_grpo(
    profile: GRPOProfile,
    *,
    mode: Literal["qa", "env"] = "qa",
    dataset: Dataset | None = None,
    engagement_id: str | None = None,
    tool_budget: int = 8,
    k: int = 10,
    dataset_path: str | None = None,
) -> str:
    """Run GRPO in either mode and return the output directory.

    Args:
        profile: The run configuration.
        mode: `"qa"` (supported) or `"env"` (experimental) -- see the module
            docstring for what each can and cannot teach.
        dataset: Prompt dataset; built from `engagement_id`/`dataset_path`
            when omitted.
        engagement_id: Required in `env` mode (the environment needs a
            graph); optional in `qa` mode.
        tool_budget: Per-episode tool-call ceiling, `env` mode only.
        k: Retrieval depth when building the dataset.
        dataset_path: JSONL from `rft-submit build-dataset`.

    Returns:
        `profile.output_dir`.

    Raises:
        ValueError: if `env` mode is requested without an engagement id.
    """
    _require_train_extra("trl")
    from trl import GRPOTrainer  # noqa: PLC0415

    if dataset is None:
        dataset = build_qa_grpo_dataset(engagement_id=engagement_id, k=k, path=dataset_path)

    trainer_kwargs: dict[str, Any] = {}
    if mode == "env":
        if not engagement_id:
            msg = "mode='env' needs --engagement-id: the environment queries a real graph"
            raise ValueError(msg)
        reward_funcs: list[Callable[..., list[float]]] = [env_composite_reward]
        weights = [1.0]
        trainer_kwargs["environment_factory"] = lambda: GrpoRolloutEnvironment(
            engagement_id=engagement_id, tool_budget=tool_budget
        )
    else:
        reward_funcs, weights = qa_reward_funcs()

    args = build_grpo_config(profile, weights)
    if profile.use_lora:
        trainer_kwargs["peft_config"] = build_lora_config()

    trainer = GRPOTrainer(
        model=profile.model_id,
        reward_funcs=reward_funcs,
        args=args,
        train_dataset=dataset,
        **trainer_kwargs,
    )
    log.info(
        "grpo_training_start",
        mode=mode,
        model_id=profile.model_id,
        n_prompts=len(dataset),
        reward_funcs=[f.__name__ for f in reward_funcs],
    )
    trainer.train()
    trainer.save_model(profile.output_dir)
    log.info("grpo_training_done", output_dir=profile.output_dir)
    return profile.output_dir
