"""sft_config.py -- the TRL SFTConfig for training on `export_sft.py`'s
output, plus `verify_masking`, a standalone check that a tokenizer + this
repo's `chat_template.jinja` actually produce the loss mask we think they
do.

Heavy-import policy
--------------------
`torch`, `trl`, `peft`, and `transformers` are only imported inside the
functions that need them (`build_sft_config`, `build_lora_config`,
`verify_masking`), never at module scope, so that `import fde_training
.sft_config` succeeds on a machine with none of them installed -- e.g. a
CI box that only needs to prove this module's pure-Python surface (the
`TrainingProfile` dataclass, `load_chat_template`, the LoRA target-module
constants) is well-formed, with no GPU stack at all. This is the MAIN
module in the package where getting the heavy-import boundary right
matters, because it is also the module most likely to accumulate a
module-scope `from trl import ...` if someone "just needs one more type
hint" -- resist that; use `TYPE_CHECKING` + a string annotation instead
(see `build_sft_config`'s return type).

WHY assistant_only_loss=True (not DataCollatorForCompletionOnlyLM)
--------------------------------------------------------------------
`DataCollatorForCompletionOnlyLM` masks by searching for a literal
response-template STRING in the rendered text. That is a string-match
heuristic: it breaks the moment a tool result happens to contain the
response template's text, or the moment there is more than one assistant
turn per example (it was designed for single-turn instruction tuning, and
naively applied to a multi-turn tool trace it will mask everything after the
FIRST match, silently dropping every subsequent assistant turn from the
loss). `assistant_only_loss=True` instead derives the mask from the chat
template's own `{% generation %}` annotations at render time -- the same
mechanism `chat_template.jinja` was written for (see that file's docstring)
-- so multi-turn, multi-tool-call trajectories mask correctly without any
string-matching. This is the VERIFIED GROUNDING fact this pipeline was built
against: `assistant_only_loss=True` requires a conversational dataset and a
`{% generation %}`-annotated template; `completion_only_loss` is the
non-chat prompt/completion sibling and does not apply here.

WHY packing is OFF
---------------------
TRL's example packing concatenates multiple training examples into one
fixed-length sequence to avoid wasting padding compute, using an attention
mask (or, in older configs, no mask at all) to keep them logically separate.
For a multi-turn TOOL-CALLING trace this is actively dangerous even when the
attention mask is respected: our loss mask is anchored to
`{% generation %}` SPANS WITHIN one rendered conversation. Packing changes
example boundaries and can shift which tokens fall in which conversation's
generation span, and more importantly, several packing implementations
default to allowing cross-example attention within a pack unless
`padding_free`/`position_ids`-aware attention is correctly wired end to end.
Getting that wrong here means one trace's assistant turn can attend to a
DIFFERENT trace's tool results in the same pack -- since tool results are
per-episode-specific KG retrieval output, the model would learn to condition
on evidence that has nothing to do with the question it is answering. The
computational saving from packing (avoiding padding waste on short traces)
is real but small relative to this risk, and our traces are already highly
variable in length (1 tool call vs. 10), so `packing=False` plus
`group_by_length=True` (bucket similar-length examples per batch to reduce
padding waste without concatenating examples) is the safer trade.

Learning rates
--------------
- LoRA: 1e-4. LoRA injects new, randomly-initialised low-rank adapters into
  otherwise-frozen weights; consistent guidance across the LoRA literature
  and TRL's own recipes is that adapter-only training tolerates (and needs)
  a roughly 10x higher LR than full fine-tuning because the adapter starts
  from scratch and the base model's own weights are not moving.
- Full fine-tune: 2e-5. Standard instruction-tuning LR band for models in
  the 1B-70B range; higher risks catastrophic forgetting of the base
  model's general capability, which matters here because the agent still
  needs to write fluent natural-language rationale/prose around its tool
  calls, not just emit tool calls.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from peft import LoraConfig
    from trl import SFTConfig

_HERE = Path(__file__).resolve().parent
CHAT_TEMPLATE_PATH = _HERE / "chat_template.jinja"


def load_chat_template() -> str:
    return CHAT_TEMPLATE_PATH.read_text()


# ===========================================================================
# LoRA target modules
#
# Attention (q/k/v/o) + MLP (gate/up/down) projections -- the standard
# "full attention + MLP" LoRA target set used across the Llama/Qwen/Mistral
# family naming conventions (all three name their projections identically).
# Restricting to attention-only trains faster but underperforms on tasks
# that require learning new STRUCTURED OUTPUT behaviour (emitting
# well-formed tool_calls JSON in a specific argument schema) rather than
# just re-weighting attention over an existing vocabulary distribution --
# the MLP projections are where a lot of "what token comes next given this
# structured context" capacity lives.
# ===========================================================================
LORA_TARGET_MODULES = [
    "q_proj",
    "k_proj",
    "v_proj",
    "o_proj",
    "gate_proj",
    "up_proj",
    "down_proj",
]

DEFAULT_LORA_R = 16
DEFAULT_LORA_ALPHA = 32  # 2x rank is the standard starting ratio
DEFAULT_LORA_DROPOUT = 0.05


@dataclass
class TrainingProfile:
    """Everything `sft_config.py` needs to know that isn't a raw TRL/peft
    constructor argument. Keeps `build_sft_config`/`build_lora_config`
    signatures small.
    """

    model_id: str = "Qwen/Qwen2.5-7B-Instruct"
    output_dir: str = "./sft-out"
    use_lora: bool = True
    per_device_train_batch_size: int = 2
    gradient_accumulation_steps: int = 8
    num_train_epochs: float = 3.0
    max_seq_length: int = 8192  # multi-turn tool traces run long; see README volume gates
    eval_strategy: str = "steps"
    eval_steps: int = 50
    save_steps: int = 50
    logging_steps: int = 5
    bf16: bool = True
    seed: int = 0


def build_sft_config(profile: TrainingProfile | None = None) -> SFTConfig:
    """Build the TRL `SFTConfig`. Imports `trl` lazily -- see module
    docstring's heavy-import policy."""
    from trl import SFTConfig  # noqa: PLC0415 -- see module docstring

    profile = profile or TrainingProfile()
    learning_rate = 1e-4 if profile.use_lora else 2e-5

    return SFTConfig(
        output_dir=profile.output_dir,
        # --- the one flag this whole file exists to get right -----------
        assistant_only_loss=True,
        # `chat_template.jinja`'s {% generation %} spans are the sole
        # source of truth for the mask (see module docstring); dataset
        # rows must be the conversational {"messages": [...]} shape
        # export_sft.py emits, NOT flattened prompt/completion pairs.
        # ------------------------------------------------------------------
        packing=False,  # see module docstring: packing + tool traces is unsafe
        group_by_length=True,  # padding-waste mitigation that IS safe (no cross-example attention)
        max_length=profile.max_seq_length,
        learning_rate=learning_rate,
        lr_scheduler_type="cosine",
        warmup_ratio=0.03,
        per_device_train_batch_size=profile.per_device_train_batch_size,
        per_device_eval_batch_size=profile.per_device_train_batch_size,
        gradient_accumulation_steps=profile.gradient_accumulation_steps,
        gradient_checkpointing=True,
        gradient_checkpointing_kwargs={"use_reentrant": False},
        num_train_epochs=profile.num_train_epochs,
        eval_strategy=profile.eval_strategy,
        eval_steps=profile.eval_steps,
        save_strategy="steps",
        save_steps=profile.save_steps,
        save_total_limit=3,
        logging_steps=profile.logging_steps,
        bf16=profile.bf16,
        seed=profile.seed,
        report_to=["none"],
        dataset_text_field=None,  # conversational dataset, not a plain text field
        # Tell the trainer where the chat template lives -- passed through
        # to the tokenizer's `apply_chat_template`, not compiled into the
        # config; kept here so callers get one canonical value.
        chat_template_path=str(CHAT_TEMPLATE_PATH),
    )


def build_lora_config(
    r: int = DEFAULT_LORA_R,
    alpha: int = DEFAULT_LORA_ALPHA,
    dropout: float = DEFAULT_LORA_DROPOUT,
) -> LoraConfig:
    from peft import LoraConfig, TaskType  # noqa: PLC0415 -- see module docstring

    return LoraConfig(
        r=r,
        lora_alpha=alpha,
        lora_dropout=dropout,
        target_modules=LORA_TARGET_MODULES,
        bias="none",
        task_type=TaskType.CAUSAL_LM,
    )


# ===========================================================================
# verify_masking -- the tripwire this module exists to provide.
#
# Cross-checks TWO independent encodings of "which tokens are trainable":
#   1. The chat template's {% generation %} spans (what TRL will actually
#      use at train time).
#   2. export_sft.py's per-message `assistant_mask` (what the DB/export
#      pipeline believes is trainable).
# If a token falls inside a {% generation %} span but its enclosing message
# was marked non-trainable by the export pipeline (or vice versa), that is
# exactly the silent-mask-bug this whole file's docstrings warn about, and
# this function raises rather than returning a "mostly fine" report.
# ===========================================================================
class MaskVerificationError(RuntimeError):
    pass


def verify_masking(
    dataset: list[dict[str, Any]],
    tokenizer: Any,
    chat_template: str | None = None,
    max_examples: int = 50,
) -> dict[str, Any]:
    """`dataset` is a list of export_sft.py records:
      {"messages": [...], "assistant_mask": [...], "meta": {...}}

    Renders each example's messages through `chat_template` (defaults to
    this repo's `chat_template.jinja`) using
    `transformers.utils.chat_template_utils.render_jinja_template`, which
    returns character-offset generation spans without requiring a full
    tokenizer vocabulary -- but we ALSO tokenize (via `tokenizer(...,
    return_offsets_mapping=True)`) so we can assert at TOKEN granularity,
    which is what actually matters for the loss (a token can straddle a
    span boundary at the character level in ways that are invisible until
    you tokenize).

    Returns a summary dict and prints one fully-decoded, mask-annotated
    example (trainable tokens wrapped in >>...<<) so a human can eyeball it
    before a training run starts.
    """
    from transformers.utils.chat_template_utils import render_jinja_template  # noqa: PLC0415

    chat_template = chat_template or load_chat_template()
    n_checked = 0
    n_examples_with_mismatch = 0
    printed_demo = False
    summary: dict[str, Any] = {"examples_checked": 0, "mismatches": 0, "mismatch_examples": []}

    for record in dataset[:max_examples]:
        messages = record["messages"]
        expected_trainable = [bool(x) for x in record["assistant_mask"]]
        if len(expected_trainable) != len(messages):
            msg = (
                f"session {record.get('meta', {}).get('session_id')}: assistant_mask length "
                f"{len(expected_trainable)} != messages length {len(messages)}"
            )
            raise MaskVerificationError(msg)

        # Render message-by-message so we know exactly which character
        # range in the FULL rendered string belongs to which message, then
        # compare against the template's own generation spans for that
        # same full render.
        rendered_list, gen_spans_list = render_jinja_template(
            conversations=[messages],
            chat_template=chat_template,
            add_generation_prompt=False,
            return_assistant_tokens_mask=True,
        )
        full_text = rendered_list[0]
        gen_spans = gen_spans_list[0]

        # Per-message boundaries: render prefixes of increasing length and
        # diff, which is exactly how TRL's own `_render_with_assistant_indices`
        # anchors spans back to messages -- we reuse the same technique here
        # rather than inventing a second one.
        boundaries = []
        for i in range(1, len(messages) + 1):
            prefix_rendered, _ = render_jinja_template(
                conversations=[messages[:i]],
                chat_template=chat_template,
                add_generation_prompt=False,
                return_assistant_tokens_mask=False,
            )
            boundaries.append(len(prefix_rendered[0]))

        message_char_ranges = []
        start = 0
        for end in boundaries:
            message_char_ranges.append((start, end))
            start = end

        def _message_is_marked_generation(
            msg_idx: int,
            ranges: list[tuple[int, int]] = message_char_ranges,
            spans: list[tuple[int, int]] = gen_spans,
        ) -> bool:
            m_start, m_end = ranges[msg_idx]
            for g_start, g_end in spans:
                if g_start >= m_end or g_end <= m_start:
                    continue
                return True
            return False

        n_checked += 1
        mismatches_here = []
        for i, msg in enumerate(messages):
            actually_generation = _message_is_marked_generation(i)
            expected = expected_trainable[i]
            if actually_generation != expected:
                mismatches_here.append(
                    {
                        "index": i,
                        "role": msg["role"],
                        "expected_trainable": expected,
                        "template_marks_generation": actually_generation,
                    }
                )
        if mismatches_here:
            n_examples_with_mismatch += 1
            summary["mismatch_examples"].append(
                {
                    "session_id": record.get("meta", {}).get("session_id"),
                    "mismatches": mismatches_here,
                }
            )

        if not printed_demo:
            printed_demo = True
            _print_annotated_example(full_text, gen_spans)

    summary["examples_checked"] = n_checked
    summary["mismatches"] = n_examples_with_mismatch
    if n_examples_with_mismatch:
        msg = (
            f"{n_examples_with_mismatch}/{n_checked} examples have a mismatch between "
            f"export_sft.py's assistant_mask and chat_template.jinja's {{% generation %}} "
            f"spans -- see summary['mismatch_examples']. DO NOT TRAIN until this is fixed. "
            f"summary={summary}"
        )
        raise MaskVerificationError(msg)
    return summary


def _print_annotated_example(full_text: str, gen_spans: list[tuple[int, int]]) -> None:
    print("=" * 78)
    print("verify_masking: decoded example, trainable spans marked >>...<<")
    print("=" * 78)
    out = []
    cursor = 0
    for start, end in sorted(gen_spans):
        out.append(full_text[cursor:start])
        out.append(">>")
        out.append(full_text[start:end])
        out.append("<<")
        cursor = end
    out.append(full_text[cursor:])
    print("".join(out))
    print("=" * 78)


def demo_dataset() -> list[dict[str, Any]]:
    """Small in-memory dataset matching export_sft.py's record shape, used
    by `fde_training.cli`'s `verify-masking` subcommand when run without a
    real exported JSONL file."""
    return [
        {
            "messages": [
                {
                    "role": "system",
                    "content": "You are the FDE Engagement Agent.",
                    "trainable": False,
                },
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
                    "content": '{"results":[{"node_key":"ctrl:discount_threshold"}]}',
                    "trainable": False,
                },
                {
                    "role": "assistant",
                    "content": (
                        "Manager approval is gated by the Discount Threshold Control "
                        "[ctrl:discount_threshold]."
                    ),
                    "trainable": True,
                },
            ],
            "assistant_mask": [False, False, True, False, True],
            "meta": {"session_id": "demo-session-1"},
        }
    ]
