"""Regenerate `tests/fixtures/tiny_tokenizer/` -- a tiny, committed, fast
tokenizer for the masking tests.

Why a committed fixture instead of `AutoTokenizer.from_pretrained("gpt2")`
---------------------------------------------------------------------------
`verify_masking`'s token-level phase needs a FAST tokenizer, because offset
mappings are the entire mechanism. Downloading one from the HF Hub inside a
test makes the suite depend on network egress and on a third party's rate
limits, which is the wrong trade for a check whose whole job is to be a
cheap always-on tripwire. So a ~500-token byte-level BPE is trained once,
here, over the text this package's own demo dataset renders to, and the
result is committed. `gpt2` remains the CLI default (`fde-training
verify-masking --tokenizer`) for humans running the command by hand.

Byte-level BPE is chosen because it has no unknown token: every byte is in
the vocabulary, so a tiny vocab degrades to near-character granularity on
unseen text rather than collapsing it to `<unk>` -- which would destroy
exactly the offsets the check reads.

Run (needs the `train` extra, or any environment with `tokenizers`
installed):

    uv run --extra train python packages/fde-training/tests/fixtures/make_tiny_tokenizer.py

Regenerate only when the demo dataset's vocabulary changes meaningfully;
the committed output is deterministic for a fixed corpus and vocab size.
"""

from __future__ import annotations

import json
from pathlib import Path

from fde_training import sft_config

OUT_DIR = Path(__file__).resolve().parent / "tiny_tokenizer"
VOCAB_SIZE = 512

# The template's structural markers. Trained as ordinary BPE merges rather
# than added as special tokens on purpose: added tokens are reported with a
# zero-length or whole-token offset by some fast tokenizers, and the masking
# check needs every structural marker to carry a real character span so a
# leak across a `<|im_start|>assistant` boundary is visible.
_EXTRA_CORPUS = [
    "<|im_start|>system\n<|im_end|>",
    "<|im_start|>user\n<|im_end|>",
    "<|im_start|>assistant\n<|im_end|>",
    "<|im_start|>tool\n<|im_end|>",
    '<|tool_call|>{"id": "call_1", "type": "function", "function": {"name": "kg_search"}}<|/tool_call|>',
    '{"tool_call_id": "call_1", "name": "kg_search", "content": "{}"}',
    "kg_search kg_traverse kg_get_node kg_propose node_key rrf_score provenance",
]


def _corpus() -> list[str]:
    """Every string the fixture tokenizer should have seen: the demo
    dataset rendered through this repo's own chat template, plus the
    structural markers above."""
    from transformers.utils.chat_template_utils import render_jinja_template  # noqa: PLC0415

    template = sft_config.load_chat_template()
    texts = list(_EXTRA_CORPUS)
    for record in sft_config.demo_dataset():
        rendered, _ = render_jinja_template(
            conversations=[record["messages"]],
            chat_template=template,
            add_generation_prompt=False,
            return_assistant_tokens_mask=True,
        )
        texts.append(rendered[0])
    return texts


def main() -> int:
    from tokenizers import Tokenizer, decoders, models, pre_tokenizers, trainers  # noqa: PLC0415

    tokenizer = Tokenizer(models.BPE())
    tokenizer.pre_tokenizer = pre_tokenizers.ByteLevel(add_prefix_space=False)
    tokenizer.decoder = decoders.ByteLevel()
    trainer = trainers.BpeTrainer(
        vocab_size=VOCAB_SIZE,
        show_progress=False,
        initial_alphabet=pre_tokenizers.ByteLevel.alphabet(),
        special_tokens=["<|endoftext|>"],
    )
    tokenizer.train_from_iterator(_corpus(), trainer=trainer)

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    tokenizer.save(str(OUT_DIR / "tokenizer.json"))
    (OUT_DIR / "tokenizer_config.json").write_text(
        json.dumps(
            {
                "tokenizer_class": "PreTrainedTokenizerFast",
                "model_max_length": 8192,
                "eos_token": "<|endoftext|>",
                "pad_token": "<|endoftext|>",
                "clean_up_tokenization_spaces": False,
            },
            indent=2,
        )
        + "\n"
    )
    print(f"wrote {OUT_DIR} (vocab_size={tokenizer.get_vocab_size()})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
