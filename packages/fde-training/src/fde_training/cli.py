"""cli.py -- one operator entrypoint for the whole training pipeline.

Design
------
Every module in this package used to carry its own `argparse`-based
`main()`/`if __name__ == "__main__":` block. That worked, but it meant six
slightly different conventions for "how do I run this" (`python
export_sft.py --stats` vs. `python rival_grader.py tournament --judge
mock`), six copies of `logging.basicConfig(...)`, and no single `--help`
that shows an operator everything this package can do. `fde-training` (see
this package's `pyproject.toml`) is the one binary; every subcommand below
delegates to the same library functions the split modules already expose --
this file adds NO new business logic beyond argument parsing, file I/O for
CLI-supplied paths, and `print()` for the operator-facing output (`T20` is
per-file-ignored for `packages/fde-training/src/**` for exactly this
reason: printing IS this file's job).

Heavy imports stay lazy even here: `verify-masking` is the one subcommand
that needs `transformers`, and it imports `AutoTokenizer` inside its own
handler function, not at module scope, so `fde-training --help` (and every
other subcommand) works on a machine with no `train` extra installed.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from fde_mcp.logging import configure_logging, get_logger
from fde_training import bedrock_rft, common, export_sft, generate_traces, rival_grader, rollout_env
from fde_training.rewards import DEFAULT_WEIGHTS, RewardHackingMonitor, compose_rewards

log = get_logger(__name__)


def _write_jsonl(records: list[dict[str, Any]], path: str) -> None:
    with Path(path).open("w") as f:
        for r in records:
            f.write(json.dumps(r) + "\n")


# ===========================================================================
# export-sft
# ===========================================================================
def _cmd_export_sft_pairs(args: argparse.Namespace) -> int:
    pairs = export_sft.build_preference_pairs()
    if not args.out:
        print(f"{len(pairs)} preference pairs (use --out PATH to write JSONL)")
        return 0
    _write_jsonl(pairs, args.out[0])
    log.info("preference_pairs_written", n_pairs=len(pairs), path=args.out[0])
    return 0


def _cmd_export_sft_records(args: argparse.Namespace) -> int:
    rows_by_session = export_sft.fetch_rows()

    if args.stats:
        export_sft.print_stats(rows_by_session)
        return 0

    records, build_stats = export_sft.build_records(rows_by_session)
    log.info("export_sft_stats", **build_stats)

    if not args.out:
        print(f"{len(records)} records built (use --out PATH... to write JSONL, or --stats)")
        return 0

    if len(args.out) == 1:
        _write_jsonl(records, args.out[0])
        log.info("sft_records_written", n_records=len(records), path=args.out[0])
        return 0

    if len(args.out) != 3:
        print("--out expects 1 path or exactly 3 (train validation test)")
        return 2

    by_split: dict[str, list[dict[str, Any]]] = {"train": [], "validation": [], "test": []}
    for r in records:
        by_split[r["meta"]["split"]].append(r)

    for path, split in zip(args.out, ("train", "validation", "test"), strict=True):
        _write_jsonl(by_split.get(split, []), path)
        log.info(
            "sft_split_written", split=split, n_records=len(by_split.get(split, [])), path=path
        )

    return 0


def _cmd_export_sft(args: argparse.Namespace) -> int:
    if args.pairs:
        return _cmd_export_sft_pairs(args)
    return _cmd_export_sft_records(args)


# ===========================================================================
# rewards-test -- sanity-check the composite reward against a small
# hand-built or exported dataset, printing the per-term breakdown a real
# GRPO run would log every step (see rewards.compose_rewards's docstring
# for why the breakdown, not just the combined scalar, is what matters).
# ===========================================================================
def _cmd_rewards_test(args: argparse.Namespace) -> int:
    if args.dataset:
        with Path(args.dataset).open() as f:
            records = [json.loads(line) for line in f if line.strip()]
        completions = [r["messages"] for r in records]
    else:
        completions = [_demo_reward_completion()]

    combined, per_term = compose_rewards(completions)
    monitor = RewardHackingMonitor()
    monitor.observe(0, completions)

    print(f"scored {len(completions)} completion(s) against DEFAULT_WEIGHTS={DEFAULT_WEIGHTS}")
    for i, score in enumerate(combined):
        print(
            f"  [{i}] combined={score:.4f} "
            + " ".join(f"{k}={v[i]:.3f}" for k, v in per_term.items())
        )
    print("monitor summary:", monitor.summary())
    return 0


def _demo_reward_completion() -> list[dict[str, Any]]:
    return [
        {"role": "user", "content": "What gates manager approval?"},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {
                        "name": "kg_search",
                        "arguments": '{"query": "approval control", "k": 5}',
                    },
                }
            ],
        },
        {
            "role": "tool",
            "tool_call_id": "call_1",
            "name": "kg_search",
            "content": '{"results": [{"node_key": "ctrl:discount_threshold"}]}',
        },
        {
            "role": "assistant",
            "content": "Manager approval is gated by [ctrl:discount_threshold].",
        },
    ]


# ===========================================================================
# verify-masking
# ===========================================================================
def _cmd_verify_masking(args: argparse.Namespace) -> int:
    from fde_training import sft_config  # noqa: PLC0415 -- keep the top-level CLI import light

    if args.dataset:
        with Path(args.dataset).open() as f:
            dataset = [json.loads(line) for line in f if line.strip()]
    else:
        dataset = sft_config.demo_dataset()

    from transformers import AutoTokenizer  # noqa: PLC0415 -- heavy import, CLI-handler-local only

    tokenizer = AutoTokenizer.from_pretrained(args.tokenizer)
    summary = sft_config.verify_masking(dataset, tokenizer)
    print("verify_masking summary:", summary)
    print(
        "OK: every message's assistant_mask agrees with chat_template.jinja's {% generation %} spans."
    )
    return 0


# ===========================================================================
# rival-grader {run, calibrate, leaderboard}
# ===========================================================================
def _cmd_rival_grader_run(args: argparse.Namespace) -> int:
    judge_fn = (
        rival_grader.MockJudge()
        if args.judge == "mock"
        else (lambda p: rival_grader.bedrock_judge(p, model_id=args.judge_model))
    )
    embed_fn = rival_grader.default_embed_fn()
    with common.connect() as db:
        cur = db.cursor()
        cur.execute("SELECT query_id FROM trn.eval_query ORDER BY query_id")
        query_ids = [r["query_id"] for r in cur.fetchall()]
        cur.execute("SELECT variant_id FROM trn.retriever_variant ORDER BY variant_id")
        variant_ids = [r["variant_id"] for r in cur.fetchall()]
    n_run = 0
    for qid in query_ids:
        for i, va in enumerate(variant_ids):
            for vb in variant_ids[i + 1 :]:
                rival_grader.run_duel(
                    qid, va, vb, judge_fn, judge_model=args.judge_model, embed_fn=embed_fn, k=args.k
                )
                n_run += 1
    print(f"ran {n_run} duel pairs ({n_run * 2} trn.duel rows)")
    return 0


def _cmd_rival_grader_calibrate(args: argparse.Namespace) -> int:
    human_labels = None
    if args.human_labels_file:
        with Path(args.human_labels_file).open() as f:
            raw = json.load(f)
        human_labels = {int(k): (int(v) if v is not None else None) for k, v in raw.items()}
    result = rival_grader.calibrate(args.judge_model, n=args.n, human_labels=human_labels)
    print(json.dumps(result, indent=2, default=str))
    return 0


def _cmd_rival_grader_leaderboard(args: argparse.Namespace) -> int:
    rows = rival_grader.leaderboard(args.iterations)
    print(f"{'name':<20}{'strength':>10}{'elo':>8}{'wins':>6}{'losses':>8}{'ties':>6}")
    for r in rows:
        print(
            f"{r['name']:<20}{r['strength']:>10.4f}{r['elo']:>8.1f}{r['wins']:>6}{r['losses']:>8}{r['ties']:>6}"
        )
    return 0


# ===========================================================================
# generate-traces
# ===========================================================================
def _cmd_generate_traces(args: argparse.Namespace) -> int:
    import random  # noqa: PLC0415 -- only the CLI handler needs a seeded RNG

    seeds = generate_traces.load_seeds_from_eval_query(args.engagement_id)
    if not seeds:
        print("no seed questions found in trn.eval_query -- nothing to sample")
        return 0

    teacher_fn = (
        generate_traces.heuristic_teacher_fn(random.Random(args.seed))  # noqa: S311 -- reproducible sampling, not crypto
        if args.teacher == "heuristic"
        else generate_traces.bedrock_teacher_fn(args.judge_model)
    )
    embed_fn = (
        rollout_env.deterministic_fake_embed
        if args.teacher == "heuristic"
        else rollout_env.default_embed_fn
    )

    stats = generate_traces.generate(seeds, args.n_samples, teacher_fn, embed_fn)
    print(
        json.dumps(
            {
                "attempted": stats.attempted,
                "accepted": stats.accepted,
                "acceptance_rate": stats.accepted / stats.attempted if stats.attempted else 0.0,
                "rejected_ungrounded": stats.rejected_ungrounded,
                "rejected_schema_invalid": stats.rejected_schema_invalid,
                "rejected_wrong_answer": stats.rejected_wrong_answer,
                "rejected_other": stats.rejected_other,
                "accepted_session_ids": stats.accepted_session_ids,
            },
            indent=2,
        )
    )
    return 0


# ===========================================================================
# rft-submit {build-dataset, build-job-config}
# ===========================================================================
def _cmd_rft_build_dataset(args: argparse.Namespace) -> int:
    rows = bedrock_rft.build_dataset(engagement_id=args.engagement_id, k=args.k)
    bedrock_rft.write_dataset(rows, args.out, allow_out_of_band_size=args.i_know_what_im_doing)
    return 0


def _cmd_rft_build_job_config(args: argparse.Namespace) -> int:
    config = bedrock_rft.build_job_config(
        job_name=args.job_name,
        base_model_id=args.base_model_id,
        region=args.region,
        training_dataset_s3_uri=args.training_dataset_s3_uri,
        grader_lambda_arn=args.grader_lambda_arn,
        role_arn=args.role_arn,
        output_s3_uri=args.output_s3_uri,
    )
    print(json.dumps(config, indent=2))
    if args.submit:
        result = bedrock_rft.submit_job(config, args.region)
        print(json.dumps(result, indent=2, default=str))
    return 0


# ===========================================================================
# argument parser assembly
# ===========================================================================
def _add_export_sft_parser(sub: argparse._SubParsersAction) -> None:
    p_export = sub.add_parser(
        "export-sft", help="trn.trace_step -> TRL-ready JSONL, or a DPO preference set."
    )
    p_export.add_argument("--stats", action="store_true", help="Print export statistics and exit.")
    p_export.add_argument(
        "--pairs",
        action="store_true",
        help="Export the DPO/preference dataset instead of SFT messages.",
    )
    p_export.add_argument(
        "--out",
        nargs="+",
        metavar="PATH",
        help=(
            "Output JSONL path(s). For the SFT export, either one path "
            "(everything, with meta.split telling you which bucket) or "
            "exactly three paths in order [train, validation, test]. For "
            "--pairs, a single path."
        ),
    )
    p_export.set_defaults(handler=_cmd_export_sft)


def _add_rewards_test_parser(sub: argparse._SubParsersAction) -> None:
    p_rewards = sub.add_parser(
        "rewards-test", help="Sanity-check the composite reward against a demo or exported dataset."
    )
    p_rewards.add_argument(
        "--dataset", help="JSONL file from export-sft. Defaults to a small built-in demo."
    )
    p_rewards.set_defaults(handler=_cmd_rewards_test)


def _add_verify_masking_parser(sub: argparse._SubParsersAction) -> None:
    p_verify = sub.add_parser(
        "verify-masking",
        help="Cross-check chat_template.jinja's {% generation %} spans against assistant_mask.",
    )
    p_verify.add_argument(
        "--dataset", help="JSONL file from export-sft. Defaults to a small built-in demo."
    )
    p_verify.add_argument(
        "--tokenizer",
        default="gpt2",
        help="HF tokenizer name/path used only to prove the template renders under a real "
        "tokenizer's chat template machinery.",
    )
    p_verify.set_defaults(handler=_cmd_verify_masking)


def _add_rival_grader_parser(sub: argparse._SubParsersAction) -> None:
    p_rival = sub.add_parser(
        "rival-grader", help="Retrieval-variant tournament, calibration, and leaderboard."
    )
    rival_sub = p_rival.add_subparsers(dest="rival_command", required=True)

    p_rival_run = rival_sub.add_parser(
        "run", help="Run every (query, variant-pair) duel not yet judged."
    )
    p_rival_run.add_argument("--judge", choices=["bedrock", "mock"], default="mock")
    p_rival_run.add_argument("--judge-model", default="anthropic.claude-3-5-sonnet-20241022-v2:0")
    p_rival_run.add_argument("--k", type=int, default=10)
    p_rival_run.set_defaults(handler=_cmd_rival_grader_run)

    p_rival_cal = rival_sub.add_parser(
        "calibrate", help="Sample duels for human adjudication; report kappa/position bias."
    )
    p_rival_cal.add_argument("--judge-model", required=True)
    p_rival_cal.add_argument("--n", type=int, default=30)
    p_rival_cal.add_argument("--human-labels-file", help="JSON {duel_id: variant_id_or_null}.")
    p_rival_cal.set_defaults(handler=_cmd_rival_grader_calibrate)

    p_rival_lead = rival_sub.add_parser("leaderboard", help="Print trn.bradley_terry output.")
    p_rival_lead.add_argument("--iterations", type=int, default=100)
    p_rival_lead.set_defaults(handler=_cmd_rival_grader_leaderboard)


def _add_generate_traces_parser(sub: argparse._SubParsersAction) -> None:
    p_gen = sub.add_parser("generate-traces", help="Rejection-sampling / STaR trace generation.")
    p_gen.add_argument("--engagement-id", help="Restrict seed questions to one engagement.")
    p_gen.add_argument("--n-samples", type=int, default=4, help="Rollouts per seed question.")
    p_gen.add_argument("--teacher", choices=["heuristic", "bedrock"], default="heuristic")
    p_gen.add_argument(
        "--judge-model",
        default="anthropic.claude-3-5-sonnet-20241022-v2:0",
        help="Model id for --teacher=bedrock.",
    )
    p_gen.add_argument("--seed", type=int, default=0)
    p_gen.set_defaults(handler=_cmd_generate_traces)


def _add_rft_submit_parser(sub: argparse._SubParsersAction) -> None:
    p_rft = sub.add_parser("rft-submit", help="Build a Bedrock RFT dataset and/or job config.")
    rft_sub = p_rft.add_subparsers(dest="rft_command", required=True)

    p_rft_ds = rft_sub.add_parser("build-dataset")
    p_rft_ds.add_argument("--engagement-id")
    p_rft_ds.add_argument("--k", type=int, default=10)
    p_rft_ds.add_argument("--out", required=True)
    p_rft_ds.add_argument("--i-know-what-im-doing", action="store_true")
    p_rft_ds.set_defaults(handler=_cmd_rft_build_dataset)

    p_rft_job = rft_sub.add_parser("build-job-config")
    p_rft_job.add_argument("--job-name", required=True)
    p_rft_job.add_argument(
        "--base-model-id", required=True, choices=list(bedrock_rft.SUPPORTED_MODELS)
    )
    p_rft_job.add_argument("--region", required=True)
    p_rft_job.add_argument("--training-dataset-s3-uri", required=True)
    p_rft_job.add_argument("--grader-lambda-arn", required=True)
    p_rft_job.add_argument("--role-arn", required=True)
    p_rft_job.add_argument("--output-s3-uri", required=True)
    p_rft_job.add_argument(
        "--submit", action="store_true", help="Actually call CreateModelCustomizationJob."
    )
    p_rft_job.set_defaults(handler=_cmd_rft_build_job_config)


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="fde-training", description=__doc__)
    sub = ap.add_subparsers(dest="command", required=True)

    _add_export_sft_parser(sub)
    _add_rewards_test_parser(sub)
    _add_verify_masking_parser(sub)
    _add_rival_grader_parser(sub)
    _add_generate_traces_parser(sub)
    _add_rft_submit_parser(sub)

    return ap


def main(argv: list[str] | None = None) -> int:
    configure_logging()
    parser = build_parser()
    args = parser.parse_args(argv)
    handler = args.handler
    return int(handler(args))


if __name__ == "__main__":
    raise SystemExit(main())
