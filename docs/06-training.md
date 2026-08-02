# Training: SFT → RL → Rival Graders

How to train agents to reason over the knowledge graph, and — as importantly — when
not to.

---

## 1. The premise

The HITL gates are already producing high-quality human labels as a side effect of
doing the work. Nobody has to run a labelling project.

| Gate outcome | Training signal |
|---|---|
| merged, no edits | **positive** — the trajectory and its output were correct |
| rejected | **negative** |
| **edited** | **paired preference** — `original_payload` vs `payload`, same input |
| expired | discard — absence of a decision is not a decision |
| approved in < N seconds | **discard** — a rubber stamp is not a label |

The edited case is the valuable one. It is a (rejected, chosen) pair on identical
input, which is exactly the shape DPO wants, and it comes for free from a reviewer
doing their job properly. `packages/fde-training/src/fde_training/export_sft.py --pairs` emits it.

The last row matters as much as the others. If you train on rubber stamps you are
training on noise that is *correlated with reviewer fatigue*, which is worse than
random noise because it is systematic.

---

## 2. Trace capture

Every agent invocation writes one `trn.trace_session` and N `trn.trace_step` rows.
Two columns carry the whole design:

**`base_commit_id`** — the graph state the agent could see. Without it a trace is
untrainable: you cannot reconstruct what was retrievable, so you cannot tell whether
a miss was the policy's fault or the graph's.

**`trainable`** — `false` on every `tool` and `user` turn. This is the loss mask,
computed once at capture and carried through, rather than re-derived (and
mis-derived) by each downstream consumer.

### Why the mask is the highest-stakes detail here

Train on retrieved tokens and one of two things happens:

1. Gradient noise from trying to predict content the policy has no control over.
2. Worse: the model learns to *generate* plausible-looking retrieval content instead
   of calling the tool. You have trained a hallucinator using retrieval data.

Every paper in this literature that specifies its masking does the same thing.
Search-R1 uses an explicit indicator `I(y_t) = 0` for tokens inside `<information>`
tags. R1-Searcher masks inside `<begin_of_documents>...<end_of_documents>`. verl
implements it structurally via delta-tokenization: tokenize the running conversation
with `add_generation_prompt=True` and `False`, diff the token ids, and only the
delta gets a loss mask of 1.

`packages/fde-training/src/fde_training/export_sft.py` **validates** the mask and fails loudly rather than
warning. A silent mask bug costs you a full training run and you will not find out
until eval.

---

## 3. Stage 0 — prompt and context engineering

**Do this first. Most teams should stop here.**

Before any training, exhaust:

- Retrieval tuning. `ef_search`, expansion hops, node-type filters, RRF k. Measured
  by the rival grader (§6). This is where the largest wins usually are, and it costs
  GPU-hours of zero.
- Prompt structure. The ontology in context, worked examples of good traversals,
  explicit tool-selection guidance.
- Tool design. Renaming `kg_traverse`'s arguments so the model gets them right is
  cheaper than teaching it the wrong ones.

Gate to proceed: you have ≥200 traces, retrieval is tuned, and there is a
*specific*, *named* failure the prompt cannot fix. "The model would be better if we
trained it" is not a gate.

---

## 4. Stage 1 — SFT

**When:** ~350+ accepted traces. **Purpose:** teach tool-call syntax and normalised
traversal shape. Not reasoning capability.

R1-Searcher's stage 1 used **350 examples** to teach a model to invoke retrieval at
all. That is the right order of magnitude for teaching a new tool schema to a base
model that is already a competent instruction-follower. It is not tens of thousands.

### Data

`trn.sft_export` produces one row per trajectory in the OpenAI messages shape —
because that is what TRL's `SFTTrainer` with chat templates and verl's multi-turn
rollout both consume. Storing it in any other shape means writing a converter, and
converters are where masking bugs live.

`export_sft.py` additionally:

- **Normalises.** Canonical tool-argument ordering; volatile fields (timestamps,
  latencies, per-run ids) stripped from tool results so the model does not memorise
  them; long results truncated under a documented policy; consecutive identical
  retrievals collapsed.
- **Dedups** on the hash of the `(tool_name, canonical_args)` sequence.
- **Splits deterministically** on a hash of `session_id`, so re-exporting does not
  shuffle train/test.

### Masking, concretely

TRL: `SFTConfig(assistant_only_loss=True)` plus a chat template annotated with
`{% generation %}...{% endgeneration %}` around assistant spans. TRL auto-patches
templates for a handful of known models; for anything else you supply it —
`packages/fde-training/src/fde_training/chat_template.jinja` is the one shipped here.

`DataCollatorForCompletionOnlyLM` is the older string-match mechanism. It breaks on
multi-turn tool traces, where multiple assistant turns interleave with tool turns
and there is no single response template to match on. Do not use it here.

`packages/fde-training/src/fde_training/sft_config.py` ships `verify_masking(dataset, tokenizer)`, which
verifies in two phases: first at message granularity (every trainable message's
rendered span is marked `{% generation %}`), then at token granularity — it
tokenizes the rendered text with `return_offsets_mapping=True` (fast tokenizer
required) and fails if any token inside a generation span belongs to a message
the export marked non-trainable. Tokens straddling a span boundary are counted
and reported, not fatal. Run it. Look at the output. It prints the decoded
example with masked tokens visibly marked, and thirty seconds of reading it is
worth more than any unit test.

### Packing must be off

Packing concatenates independent sequences to fill the context window. For
multi-turn tool traces this corrupts both the attention boundaries between
trajectories and the label mask alignment. Turn it off. The throughput loss is real
and it is not negotiable.

### LoRA vs full fine-tune

LoRA (r=16, alpha=32 — the shipped defaults in `sft_config.py`, targeting
attention + MLP projections, lr 1e-4) is right for
teaching tool-call *syntax and format* on top of a capable base. Cheap, fast to
iterate, and composes well with vLLM/SGLang colocated inference during any
subsequent RL.

Full fine-tune (lr 2e-5) only if SFT must shift the underlying reasoning
distribution — a large teacher-student capability gap, or very long multi-hop
traces. That is a bigger claim than most projects can support; start with LoRA.

One deployment consequence: Bedrock Custom Model Import documents **merged
safetensors**, not standalone LoRA adapters. If Bedrock CMI is your serving path,
plan to merge before import. (Flagged as not-fully-verified in `99-sources.md` —
confirm before you build a release process around it.)

### Trace generation when you do not have 350 traces

`packages/fde-training/src/fde_training/generate_traces.py` does rejection sampling (STaR-style): sample N
trajectories from a strong teacher against the **live environment**, keep only those
that (a) reach the gold answer, (b) have every tool call schema-valid, and (c) are
fully grounded. Survival rates around 25% are normal and are the point — most
teacher trajectories fail at least one of the three checks. (The exact count
varies run to run: ANN retrieval under `relaxed_order` is not tie-stable across
environments, so this document deliberately does not pin one. The seeded
fixture in `packages/fde-training/fixtures/eval_queries.jsonl` is asserted in
tests to produce at least one accept and at least one reject.)

---

## 5. Stage 2 — RL

**When:** ~2,400+ examples, SFT plateaued, and you can name the specific traversal
failures you are targeting. **Purpose:** fix behaviours SFT cannot, because they are
about *when to stop searching*, not *what token comes next*.

s3 used **2,400 examples** with PPO and beat baselines trained on 70× more data.
R1-Searcher's stage 2 used **~8,148**. AWS's own guidance for Bedrock RFT is to
start with **100–200 prompts**. Anything claiming you need 100k+ examples for RL
here is not supported by the current search-agent RL literature — sample efficiency
is the whole point of RLVR once the reward is verifiable and the masking is right.

### What RL is pointed at

`trn.traversal_failure` enumerates the target set explicitly, and each kind maps to
a reward term:

| Failure | What it looks like |
|---|---|
| `empty_result` | filter/type mistake returns nothing |
| `wrong_entry_point` | seeded on the wrong node; everything downstream is off |
| `under_retrieval` | stopped one hop short of the answer |
| `over_retrieval` | pulled 400 nodes to answer a 2-node question |
| `wrong_edge_type` | followed `precedes` where the question needed `depends_on` |
| `schema_invalid` | arguments the DB would reject |
| `cycle_thrash` | re-visited the same subgraph repeatedly |
| `ungrounded_claim` | asserted something absent from every retrieved row |
| `stale_commit` | reasoned over an old commit without saying so |
| `hop_budget_exceeded` | hit the ceiling and gave up |

Categorising rather than lumping these into "wrong answer" is what makes a targeted
reward possible.

### Reward composition

`packages/fde-training/src/fde_training/rewards/`. Weights are a starting point, not a law.

| Term | Weight | What it does |
|---|---|---|
| `r_grounded` | 0.30 | every claim appears in a retrieved provenance record |
| `r_outcome` | 0.25 | F1 against the gold node-key set |
| `r_schema_valid` | 0.10 | arguments the DB would accept |
| `r_hop_efficiency` | 0.10 | **two-sided** -- penalises under- AND over-retrieval |
| `r_retrieval_quality` | 0.10 | nDCG@k against pooled relevance judgements |
| `r_format` | 0.05 | tool calls parse |
| `r_citation` | 0.05 | provenance cited in the required format |
| `r_cost` | 0.05 | tokens and DB time, weighted low so it never dominates |

Source of truth is `DEFAULT_WEIGHTS` in `packages/fde-training/src/fde_training/rewards/`; the weights sum
to 1.0 and this table is checked against it in CI.

Design notes worth defending:

**Groundedness outweighs outcome, deliberately.** This is a departure from
Search-R1, and it is the most arguable weight in the table. The reasoning: a
fluent, well-cited, format-perfect answer that names a `node_key` nobody ever
retrieved is a worse outcome for this platform than an honest "I do not have
enough information" -- because the answer feeds a proposal that a human will
review, and a confident ungrounded claim wastes reviewer trust in a way a
refusal does not. If your downstream consumer is a person rather than a
benchmark, weight grounding above outcome.

**Outcome reward is still the second-largest term.** Search-R1 argues for pure
outcome reward and explicitly avoids format/process terms as unnecessary complexity.
That works when the tool schema is already known. It does not work here, where the
model must learn a bespoke graph-query surface — hence the small format and schema
terms, which function as curriculum: they matter early and become free later.

**`r_grounded` is the anti-hallucination term** and the reason `provenance` is
carried through every retrieval result. Without it, outcome reward alone rewards
"fortunate hallucination" — right answer, no evidence chain.

**`r_hop_efficiency` is two-sided on purpose.** This is the documented collapse
mode: GRPO agents under pure outcome reward show *decreasing* mean tool-call count
over training, because shortcut exploitation is the easiest gradient to find. A
one-sided efficiency penalty accelerates exactly that. KG-R1's turn-level query
validity and GraphRAG-R1's Progressive Retrieval Attenuation both exist to fight the
same collapse.

**`RewardHackingMonitor`** tracks, per step: mean tool calls, mean hops,
ungrounded-claim rate. It fires loudly when mean tool calls declines for N
consecutive steps. Wire it to your training dashboard and treat it as a stop
condition, not a chart.

### Algorithm

**GRPO** (`loss_type='grpo'`), `beta=0.0` (KL off by default in TRL, following
Open-Reasoner-Zero), `vllm_importance_sampling_correction=True`. Try `dapo` if
training is unstable at scale.

The field is not unanimous: Search-R1 uses PPO, R1-Searcher uses Reinforce++, s3
uses PPO for stability, KG-R1 and GraphRAG-R1 use GRPO variants. GRPO is the
reasonable default because it needs no value network and the group-relative
advantage suits a verifiable reward, but if it is unstable, PPO is a defensible
retreat rather than a failure.

### The environment must be the production environment

`packages/fde-training/src/fde_training/rollout_env.py` runs the **same** `kg.hybrid_search` against the **same**
Postgres, through a read-only `fde_rl_rollout` role, pinning a commit at `reset()`
so an episode is reproducible.

If the rollout environment differs from what production serves, you are optimising a
policy for a world it will never see. This is the most expensive mistake available
in this whole pipeline and it is completely silent — the training curves look fine.

The rollout role (`db/012`) gets strictly less than `fde_agent`: read `kg.*`, write
its own traces, **no access to `hitl.*` at all**. A rollout cannot create a
proposal, cannot submit one, and therefore cannot manufacture its own training
label. Same rule as everywhere else: the label comes from the human gate, never from
the thing being trained.

### Frameworks

| | Multi-turn tools w/ live env | Async rollout | Verdict |
|---|---|---|---|
| **verl** | yes — `rollout.multi_turn: True`, `BaseTool` subclass | yes, `AgentLoopWorker` | most mature; Search-R1 builds on it |
| **TRL `GRPOTrainer`** | yes — `tools=[...]` or `environment_factory` | reward fns may be async | lowest friction if already in HF |
| **SkyRL / rLLM** | yes, agent-focused | partial rollout | worth watching |
| **OpenRLHF** | yes, Ray-based | yes | fine, less search-specific precedent |

Start with TRL if you want to avoid standing up Ray. Move to verl when rollout
throughput becomes the bottleneck.

---

## 6. Rival graders

Two retrieval configurations answer the same question; a judge picks a winner;
aggregate with Bradley-Terry. This is how you know whether a retrieval change
actually helped, and it is worth building **before** either training stage.

### Pairwise, not pointwise

Absolute 1–10 scoring drifts badly across sessions. Relative preference is stable.
The LLM-judge survey literature is consistent on this: pairwise comparative judging
outperforms pointwise on both positional consistency and human alignment.

### Every pair runs in both orders

Position bias is real, is not random, varies by judge and task, and — the part that
matters — is **worst exactly when the two candidates are close in quality**, which
is the regime you care about. A/B-ing two similar retrievers is precisely where the
bias bites hardest.

So `rival_grader.py` runs every pair as (A,B) and (B,A), writes both rows with
`mirror_duel_id` and `consistent`, and **excludes inconsistent pairs from the
Bradley-Terry fit** rather than averaging them away. Averaging hides the problem;
excluding surfaces it. `trn.judge_position_bias()` reports the rate.

The judge prompt (versioned as a constant) forbids using result-list length or
ordering position as a signal and requires the judge to name which specific
retrieved items support its verdict. A judge that cannot point at the evidence is
guessing.

### Pooled judgements, TREC-style

`trn.eval_query.pooled_keys` accumulates the union of everything any variant has
ever returned for that query; each pooled item is judged **once** and reused across
all comparisons. This avoids the "unjudged = irrelevant" bias that systematically
makes a *new* variant look worse than an incumbent whose results were all judged
years ago.

### Calibration, and the number that surprises people

`trn.judge_kappa(judge_model)` computes Cohen's kappa against human adjudication on
a sampled subset, and returns a verdict:

| Kappa | Verdict |
|---|---|
| n < 50 | `insufficient_sample` |
| < 0.60 | `unusable: judge disagrees with humans too often` |
| 0.60–0.77 | `marginal: usable for ranking, not for RL reward` |
| **0.78–0.82** | **`calibrated: human-equivalent`** |
| > 0.82 | `suspiciously high: check for prompt artefact or trivial eval set` |

Human-to-human agreement averages about **0.801**. The target band is 0.78–0.82 —
*human-equivalent*, not perfect. A judge scoring well above that band is usually
overfit to a prompt artefact or facing an eval set too easy to discriminate. Both
extremes deserve investigation; only one of them looks like a problem on a
dashboard.

The `marginal` band is a real distinction. A judge good enough to rank two
retrievers is not automatically good enough to be an RL reward, where its errors
compound across thousands of gradient steps.

### Leaderboard

```
$ uv run fde-training rival-grader leaderboard
name                strength   elo    wins  losses  ties
rrf-k60-2hop           1.847  1606      64      18     4
high-recall-ef200      1.102  1517      41      35     6
graph-heavy-3hop       0.771  1455      29      44     3
ann-only               0.428  1352      12      61     2
```

`ann-only` is the ablation that answers the question the whole architecture rests
on: **is the graph actually adding anything over plain vector search?** Run it. If
`ann-only` is competitive, the graph is not earning its complexity for your queries
and you should know that before you train on it.

---

## 7. The AWS path

### Self-managed (SageMaker + verl/TRL)

- SageMaker training jobs or HyperPod; `aws/sagemaker-hyperpod-recipes` ships LoRA
  and QLoRA configs for Llama, DeepSeek-R1-distilled-Qwen, and GPT-OSS.
- Full control over reward functions, environment, and algorithm.
- You own the Ray/vLLM/rollout infrastructure.
- Serving: merge weights → Bedrock Custom Model Import (HF safetensors; Llama,
  Mistral/Mixtral, Qwen2/2.5/3, GPT-OSS; < 200GB text, < 128K positional
  embeddings; us-east-1, us-east-2, us-west-2, eu-central-1) or self-host on
  SageMaker endpoints.

### Bedrock Reinforcement Fine-Tuning (managed)

GRPO under the hood, stated explicitly by AWS. Custom **Lambda code graders** (must
run in seconds) or model-as-a-judge. No labelled dataset needed — a set of prompts
suffices, and AWS's guidance is to start with 100–200.

Supported base models today:

| Model ID | Region |
|---|---|
| `amazon.nova-2-lite-v1:0:256k` | us-east-1 |
| `openai.gpt-oss-20b` | us-west-2 |
| `qwen.qwen3-32b` | us-west-2 |

`packages/fde-training/src/fde_training/bedrock_rft.py` builds the job; `packages/fde-training/src/fde_training/lambda_grader.py` is the actual
Lambda handler implementing the composite reward.

**The constraint that decides it:** three fixed base models. CMI (bring-your-own
weights) and RFT (managed GRPO) look like separate feature tracks — it is *not
verified* that a CMI-imported model can be an RFT base, and the fixed model-ID list
suggests not. If you need a specific base checkpoint, self-manage.

### Choosing

| | Self-managed | Bedrock RFT |
|---|---|---|
| Base model | any | 3 fixed |
| Reward | arbitrary Python | Lambda (seconds) or judge |
| Environment | live Postgres in the loop | Lambda grader only |
| Infra to own | Ray, vLLM, rollout workers | none |
| Time to first run | weeks | days |
| Cost floor | high | low |

If your reward genuinely needs the live graph in the rollout loop — and
`r_retrieval_quality` and `r_grounded` do — self-managed is the honest answer. Use
Bedrock RFT to find out cheaply whether RL helps at all before committing.

---

## 8. Honest staged plan

| Stage | Trigger | Do | Expect |
|---|---|---|---|
| **0** | day one | prompt + retrieval tuning; stand up the rival grader | most of the available win |
| **1** | ≥200 traces | tune retrieval against the grader leaderboard | +10–25% retrieval quality |
| **2** | ≥350 accepted traces | SFT LoRA for tool syntax + traversal shape | fewer schema errors, consistent shape |
| **3** | SFT plateaued, ~2.4k examples, named failures | GRPO on the live env | targeted failure reduction |
| **4** | ≥8k, RL stable | scale RL, widen the curriculum | diminishing |

**Most teams should stop after stage 2.** Stage 3 costs weeks of engineering and
real GPU-hours, and its benefit is bounded by how good your reward function is —
which is bounded by how good your judge is, which needs kappa ≥ 0.78 to be
trustworthy as a reward at all.

The order is not negotiable either. RL on top of an untuned retriever optimises the
policy to compensate for a bad environment, and then improving the retriever later
makes the policy worse.

---

## 9. What to watch during training

| Signal | Where | Means |
|---|---|---|
| mean tool calls declining | `RewardHackingMonitor` | **shortcut exploitation — stop** |
| ungrounded-claim rate rising | `trn.trace_step.grounded` | fortunate hallucination |
| `r_outcome` up, `r_grounded` flat | reward decomposition | right answers, no evidence chain |
| `schema_invalid` not falling | `trn.failure_label` | tool schema is confusing; fix the tool, not the model |
| judge kappa drifting down | `trn.judge_kappa` | judge model changed under you, or eval set drifted |
| position-bias rate rising | `trn.judge_position_bias` | candidates converged; the judge can no longer discriminate |

That last one is a good problem. It means your variants are close enough that the
judge cannot tell them apart — at which point the retrieval work is done and the
bottleneck is somewhere else.
