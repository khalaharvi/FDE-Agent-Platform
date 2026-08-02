# Sources

Every external factual claim made across `docs/00` through `docs/10` traced
to a primary source, grouped by topic. Anything not traceable to a primary
source is called out explicitly in Section 6 rather than presented as fact.
Dates below are as given to the author at time of writing (current date:
2026-08-01); verify anything time-sensitive before quoting it in a customer
deliverable.

---

## 1. AgentCore

| Claim | Source |
|---|---|
| Amazon Bedrock AgentCore reached General Availability 2025-10-13 | https://aws.amazon.com/about-aws/whats-new/2025/10/amazon-bedrock-agentcore-available |
| Supported regions (21 listed as of the devguide's current revision) | https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/agentcore-regions.html |
| Runtime pricing: $0.0895/vCPU-hr, $0.00945/GB-hr, 1-second billing minimum | https://aws.amazon.com/bedrock/agentcore/pricing/ |
| Gateway pricing: $0.005/1,000 API invocations, $0.025/1,000 search calls, $0.02 per 100 tools indexed/month | https://aws.amazon.com/bedrock/agentcore/pricing/ |
| Memory pricing: short-term $0.25/1,000 events; long-term built-in strategies $0.75/1,000 records/month; retrieval $0.50/1,000 | https://aws.amazon.com/bedrock/agentcore/pricing/ |
| Identity pricing: $0.010/1,000 tokens | https://aws.amazon.com/bedrock/agentcore/pricing/ |
| Runtime limits: 8-hour max session, 15-minute idle/ping-reap timeout, 100MB request payload, 10MB streaming chunk, 15-minute synchronous invoke timeout, 60-minute streaming ceiling, 2 vCPU / 8GB per session, 5,000 concurrent sessions in us-east/us-west (2,500 elsewhere), 200 TPS on `InvokeAgentRuntime`, 1GB session storage | https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/bedrock-agentcore-limits.html |
| Session model: 1:1 session-to-microVM, session IDs ≥33 characters, `Mcp-Session-Id` header vs. `X-Amzn-Bedrock-AgentCore-Runtime-Session-Id` header are distinct concepts | https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-sessions.html |
| ARM64 is required for the container deployment path | https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/getting-started-custom.html |
| Runtime execution-role IAM shape | https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-permissions.html |
| Gateway core concepts (targets, protocol types) | https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/gateway-core-concepts.html |
| Gateway semantic tool search (`protocolConfiguration.mcp.searchType='SEMANTIC'`, the `x_amz_bedrock_agentcore_search` built-in tool) | https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/gateway-using-mcp-semantic-search.html |
| Memory strategies (semantic, summary, user-preference) | https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/memory-strategies.html |
| Memory namespace/actor model | https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/session-actor-namespace.html |
| Identity / workload identity setup | https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/identity-getting-started-step3.html |
| Observability (CloudWatch GenAI session/trace/span wiring) | https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/observability.html |
| Long-running tasks / HITL (`add_async_task`/`complete_async_task`, `HealthyBusy`) | https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-long-run.html |
| Step Functions `bedrockagentcore:invokeHarness` integration: Request-Response only, no `.sync`, no `waitForTaskToken`, capped at 15 minutes regardless of `TimeoutSeconds` | https://docs.aws.amazon.com/step-functions/latest/dg/connect-bedrockagentcore.html |
| `bedrock-agentcore` Python SDK source (`BedrockAgentCoreApp`, `add_async_task`, ping wiring) | https://github.com/aws/bedrock-agentcore-sdk-python/blob/main/src/bedrock_agentcore/runtime/app.py |
| `agentcore` CLI (new: `create`/`dev`/`deploy`/`invoke`) | https://github.com/aws/agentcore-cli |
| `bedrock-agentcore-starter-toolkit` (legacy CLI) | https://github.com/aws/bedrock-agentcore-starter-toolkit |
| AgentCore Evaluations reached GA, 2026-03 | https://aws.amazon.com/about-aws/whats-new/2026/03/agentcore-evaluations-generally-available |
| `CreateEvaluator` API reference | https://docs.aws.amazon.com/bedrock-agentcore-control/latest/APIReference/API_CreateEvaluator.html |

**Regions reconciliation flag:** the devguide's regions page lists 21 regions
as the source of record for this document set. A separate June 2026 AWS
post is reported to add 4 more regions on top of that baseline. This document
set treats 21 as the number as of the devguide snapshot used; **reconcile
against the live devguide page before quoting a region count in any customer
deliverable**, since AgentCore's region footprint is actively expanding and a
stale count understates availability.

---

## 2. pgvector / PostgreSQL

| Claim | Source |
|---|---|
| pgvector 0.8.2 released 2026-02-25 (security fix: parallel HNSW build buffer overflow) | https://www.postgresql.org/about/news/pgvector-082-released-3245/ |
| pgvector 0.8.0 introduced iterative index scans (`hnsw.iterative_scan`) | https://www.postgresql.org/about/news/pgvector-080-released-2952/ |
| pgvector repository / release notes | https://github.com/pgvector/pgvector |
| RDS for PostgreSQL extension-version support matrix | https://docs.aws.amazon.com/AmazonRDS/latest/PostgreSQLReleaseNotes/postgresql-extensions.html |
| RDS for PostgreSQL added pgvector 0.8.0, announcement | https://aws.amazon.com/about-aws/whats-new/2024/11/amazon-rds-for-postgresql-pgvector-080/ |
| Aurora PostgreSQL added pgvector 0.8.0, announcement | https://aws.amazon.com/about-aws/whats-new/2025/04/pgvector-0-8-0-aurora-postgresql |
| Production guidance for pgvector on Aurora PostgreSQL | https://aws.amazon.com/blogs/database/running-pgvector-in-production-on-amazon-aurora-postgresql/ |
| Apache AGE is not available on RDS/Aurora (tracked as an open request, not a supported extension) | https://github.com/apache/age/issues/998 |
| PostgreSQL 14 introduced `SEARCH`/`CYCLE` clauses for recursive CTEs | https://www.postgresql.org/docs/current/queries-with.html |
| `SEARCH`/`CYCLE` clause background and rationale | https://www.depesz.com/2021/02/04/waiting-for-postgresql-14-search-and-cycle-clauses/ |

This platform's live deployment (verified directly against the running
database, not from documentation): **pgvector 0.8.2** on **PostgreSQL
16.14** (Ubuntu build). `db/001_extensions_and_types.sql`'s minimum-version
guard (`>= 0.8.0`) is satisfied with headroom.

---

## 3. Bedrock embedding models

| Claim | Source |
|---|---|
| Titan Text Embeddings v2 model card (dimensions 256/512/1024, `normalize` parameter, 8K token context) | https://docs.aws.amazon.com/bedrock/latest/userguide/model-card-amazon-titan-text-embeddings-v2.html |
| Cohere Embed v4 on Bedrock model parameters (dimensions 256/512/1024/1536, `input_type` asymmetric parameter, output formats) | https://docs.aws.amazon.com/bedrock/latest/userguide/model-parameters-embed-v4.html |

`mcp/embeddings.py`'s request/response shapes for both families (Titan's
single-item `invoke_model` body, Cohere's up-to-96-item batch body with
`input_type`/`embedding_types`/`output_dimension`) are written directly
against these model-card documented shapes. As `mcp/README.md` states
plainly, this code path has **not** been exercised against a live Bedrock
endpoint in this environment (no AWS credentials/network egress to
`bedrock-runtime` available here) — see Section 6.

---

## 4. Training literature

| Claim / technique | Source |
|---|---|
| Search-R1 (RL for search-augmented reasoning) | arXiv:2503.09516 |
| R1-Searcher | arXiv:2503.05592 |
| ReSearch | arXiv:2503.19470 |
| DeepRetrieval | arXiv:2503.00223 |
| s3 (search-augmented RL variant) | arXiv:2505.14146 |
| Graph-R1 | arXiv:2507.21892 |
| GraphRAG-R1 | arXiv:2507.23581 |
| KG-R1 | arXiv:2509.26383 |
| DAPO (RL algorithm) | arXiv:2503.14476 |
| GSPO (RL algorithm) | arXiv:2507.18071 |
| RLOO (RL algorithm) | arXiv:2402.14740 |
| TRL `SFTTrainer` documentation (chat-template-driven SFT, the shape `trn.sft_export` is written for) | https://huggingface.co/docs/trl/en/sft_trainer |
| verl (multi-turn RL rollout framework) | https://github.com/volcengine/verl |
| Bedrock Reinforcement Fine-Tuning (managed GRPO-style alternative) | https://docs.aws.amazon.com/bedrock/latest/userguide/reinforcement-fine-tuning.html |
| Bedrock Custom Model Import | https://docs.aws.amazon.com/bedrock/latest/userguide/model-customization-import-model.html |

---

## 5. LLM-judge literature

| Claim | Source |
|---|---|
| Position bias in LLM-as-judge pairwise evaluation | arXiv:2406.07791 |
| Survey of LLM-as-judge methods and failure modes | arXiv:2411.15594 |
| Self-preference bias in LLM judges | arXiv:2410.21819 |
| Judge-vs-human agreement / Cohen's kappa calibration bands | arXiv:2510.09738 |
| RAGAS (retrieval-augmented generation evaluation framework) | https://docs.ragas.io/en/stable |

`db/009_training.sql`'s `trn.judge_kappa` calibration bands (target ~0.78–0.82,
"suspiciously high" above ~0.82) are set directly from arXiv:2510.09738's
reported human-to-human agreement range for this class of judgment task —
the function's own `verdict` column labels are the operational translation of
that literature, not an independently-derived threshold.

---

## 6. Could NOT be verified from primary sources — recheck before build

The following appear in the surrounding docs as context or are used for
reasoning, but the author could not independently confirm them against a
first-party AWS/vendor page in this environment. Treat as "reported, not
verified" and recheck before quoting externally:

- **AgentCore Runtime cold-start latency numbers.** No first-party benchmark
  page was located; any specific millisecond figure quoted elsewhere is an
  estimate, not a documented SLA.
- **Exact pgvector version shipped per RDS PostgreSQL minor version (13–16)
  as of mid-2026.** The RDS extension-version matrix (Section 2) is the
  right page to check, but it changes with each RDS engine-version release
  and was not re-checked against every minor version at time of writing —
  confirm the exact pgvector version for the specific RDS engine version you
  will deploy against, not just "RDS supports 0.8.0+."
- **Aurora Serverless v2 ACU/memory interaction with HNSW index builds.**
  No first-party guidance was found quantifying how Aurora Serverless v2's
  auto-scaling interacts with a large `maintenance_work_mem`-driven HNSW
  build (Section 6.7 of `docs/01-knowledge-graph.md`) — if deploying on
  Serverless v2 rather than provisioned Aurora, validate this empirically
  before relying on the worked `maintenance_work_mem` calculation as sized.
- **pgvectorscale and ParadeDB availability on RDS/Aurora.** Not confirmed
  either way from a first-party AWS page; do not assume either is available
  as a managed extension without checking the current RDS/Aurora extension
  list directly.
- **Titan v2's exact batch/payload limits** beyond the 8K-token context
  documented in Section 3 — the model card documents context length; total
  request payload/throughput limits for `invoke_model` were not
  independently re-verified here.
- **Cohere Embed v4's exact per-token/per-request Bedrock pricing.** Not
  independently confirmed; check the Bedrock pricing page directly at
  deployment time rather than assuming parity with Titan.
- **The full roster of AgentCore's built-in evaluators** (the "17
  evaluators" figure referenced informally in `docs/02-agent-engagement.md`
  and `docs/04-agent-development.md`). The specific evaluator IDs actually
  used in those documents (`Builtin.GoalSuccessRate`,
  `Builtin.TrajectoryInOrderMatch`, `Builtin.Faithfulness`, etc.) are
  plausible per the AgentCore Evaluations feature area, but the *complete
  enumerated list* of 17 was sourced from a third-party guide, not a
  first-party AWS enumeration page — confirm the exact current roster
  against the AgentCore Evaluations console or API before treating any
  specific evaluator ID as guaranteed to exist under that exact name.
- **Whether a Custom-Model-Import (CMI) model can serve as the base for
  Bedrock Reinforcement Fine-Tuning.** Not confirmed as supported; the two
  features' documentation does not state this combination is possible, and
  it is more likely unsupported than supported as of this writing — do not
  assume CMI → RFT is a valid pipeline without checking current Bedrock RFT
  base-model requirements directly.
- **Whether Custom Model Import accepts LoRA adapters versus requiring fully
  merged model weights.** The CMI documentation as reviewed describes merged
  safetensors artifacts; it was not confirmed whether an unmerged adapter is
  ever accepted. Assume merged-weights-only until confirmed otherwise.

---

## 7. Field notes — discovered while building this repo

Two things found empirically, not documented anywhere upstream, that would
otherwise cost someone a debugging session:

**1. pgvector compiled with the default `-march=native` crashes Postgres
with SIGILL on a heterogeneous fleet.** pgvector's `Makefile` defaults to
`-march=native`, which bakes in instruction-set extensions (AVX-512, etc.)
present on the *build* host's CPU. If the fleet running Postgres is not
uniformly the same CPU generation — a rolling replacement, a mixed-instance
Auto Scaling Group, a build host that differs from a production host — a
binary compiled with `-march=native` will `SIGILL` (signal 4) the moment it
executes an instruction the *running* host's CPU doesn't have, and this
surfaces as Postgres crashing outright, not as a graceful fallback. This was
observed directly after a host CPU generation changed under an existing
deployment. **Build with `OPTFLAGS=""`** (overriding pgvector's default) to
get a portable binary that runs correctly across a heterogeneous fleet, at
the cost of not using CPU-specific SIMD instructions pgvector could otherwise
exploit for distance computation. If every host in the fleet is verified
identical down to CPU generation, `-march=native` is safe and faster; the
default is not safe to assume in any environment where that's not explicitly
guaranteed (which includes most managed-service fleets, since the underlying
hardware generation is not a contract AWS makes).

**2. `REFRESH MATERIALIZED VIEW CONCURRENTLY` requires the calling role to
own the matview — there is no grantable `REFRESH` privilege on PostgreSQL 16
short of ownership (pre-PG17).** This is why `sor.run_all_detectors`
(`db/007_drift.sql`) is declared `SECURITY DEFINER`, owned by the migration
owner: it lets a narrowly-privileged ingest role trigger the refresh without
itself owning the object. Verified two ways during this build: (a) directly
against the live schema, calling the function as `fde_ingest` (which holds
`EXECUTE` but not ownership) succeeds and returns real per-detector counts;
(b) reproduced the underlying mechanism from scratch with a separate
non-superuser test role to confirm `SECURITY DEFINER` correctly re-resolves
*all* downstream permission checks inside the function body (not just the
`REFRESH` itself) to the definer's grants — meaning the definer role needs
read access to everything the detector queries transitively (`kg.
edge_current`, `sor.observation`, etc.), which a migration owner that
created those objects has automatically, but a `SECURITY DEFINER` owner
installed after the fact would need granted explicitly. Two related,
already-noted-in-repo but worth restating findings surfaced during the same
investigation: `mcp/README.md`'s "Known limitations" section, which
describes this matview-ownership problem as still unresolved "for ANY role,
including fde_ingest," is **stale** relative to the `SECURITY DEFINER` fix
now present in the shipped `007_drift.sql` — verified live, `fde_ingest`
succeeds. Separately, and not previously documented anywhere: **`fde_agent`
— the role the MCP server's `drift_scan` tool actually runs as — was never
granted `EXECUTE` on `sor.run_all_detectors` at all**, so `drift_scan` fails
today with `permission denied for function run_all_detectors` before it ever
reaches the matview-ownership question the README describes. The server
handles this gracefully (a structured `{error, hint}`, not a crash — see
`docs/05-mcp-surface.md` Section 2.3), but the tool is not currently
reachable as deployed; the fix is a one-line `GRANT EXECUTE ON FUNCTION
sor.run_all_detectors(uuid) TO fde_agent;`, not a further ownership change.

---

## 8. Corrections applied after the first verification pass

A skeptical verification pass against the live database found the following, all
now fixed. Recorded because the *class* of error is instructive.

| Finding | Was | Now |
|---|---|---|
| `fde_agent` lacked `EXECUTE` on `sor.run_all_detectors` | `drift_scan` failed silently inside the tool's generic error boundary -- the autonomous monitoring loop was monitoring nothing | granted in `db/011`; verified with `has_function_privilege` |
| Reward-weight table in `docs/06-training.md` | 5 of 8 weights disagreed with `DEFAULT_WEIGHTS`, including which term dominates | table regenerated from the code; `r_grounded` 0.30 > `r_outcome` 0.25 |
| Opportunity rubric in `docs/02-agent-engagement.md` | weights, band names, and override logic all disagreed with `OpportunityScore` | rewritten from `agents/common/models.py` |
| Gate-policy numbering | `db/010`'s comments numbered 1-7 for 8 rows (rule 3 is two INSERTs), so doc references were off by one | comments renumbered to match `policy_id`; doc references corrected |
| "13 migrations" in README | there are 12 | corrected |
| Guardrail tables in `docs/02/03/04` | named 19 guards; 4 exist under those names | tables kept as design intent, with an explicit note naming what is actually shipped |
| `mcp/README.md` "Known limitations" | claimed the matview-ownership issue was unfixed | superseded by `SECURITY DEFINER` on `sor.run_all_detectors`; see §7 |

### Sources for numbers cited in `docs/09-deployment.md`

Added here because the exhaustive-sourcing claim above requires it:

- Gateway limits (100 targets/gateway, 1,000 tools/target, 6 MB tool payload,
  15-minute gateway timeout), runtime versions/endpoints per agent (1,000 / 10):
  https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/bedrock-agentcore-limits.html
- Code Interpreter billing (per vCPU-hour / GB-hour, 128 MB memory floor) and
  Gateway per-invocation pricing: https://aws.amazon.com/bedrock/agentcore/pricing/
- Code Interpreter session timeouts (15 min default, 8 hr max):
  https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/code-interpreter-getting-started.html
- Aurora PostgreSQL minimum versions for pgvector 0.8.0 (16.8 / 15.12 / 14.17 /
  13.20): https://aws.amazon.com/about-aws/whats-new/2025/04/pgvector-0-8-0-aurora-postgresql
- RDS PostgreSQL minimum versions for pgvector 0.8.0 (17.1 / 16.5 / 15.9 / 14.14 /
  13.17): https://aws.amazon.com/about-aws/whats-new/2024/11/amazon-rds-for-postgresql-pgvector-080/
- Aurora instance-class guidance for resident HNSW graphs:
  https://aws.amazon.com/blogs/database/running-pgvector-in-production-on-amazon-aurora-postgresql/

The monthly cost figures in `docs/09-deployment.md` §9 are **arithmetic from the
published unit prices above applied to a stated hypothetical workload**, not an
AWS-published estimate. Treat them as an order-of-magnitude sketch and re-derive
against the pricing calculator for any real budget.
