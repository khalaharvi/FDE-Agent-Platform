# Deployment

AgentCore runtimes, IAM, database, CI/CD, cost, and the order to build in.

---

## 1. Build order

Do not build this in the order of the architecture diagram. Build it in the order
that lets you find out you were wrong cheaply.

| Phase | Build | Done when | Est. |
|---|---|---|---|
| **0** | Aurora + migrations `001`–`012`; `rebuild.sh` green in CI | 16 smoke tests pass on a clean DB | 2–3 d |
| **1** | Gate service + a minimal review UI | a human can approve a hand-written proposal and see the commit | 1 w |
| **2** | KG MCP server + embedder worker, run locally over stdio | `kg_search` returns fused results with provenance | 1 w |
| **3** | Engagement Agent on AgentCore Runtime | a real interview becomes a merged commit | 1–2 w |
| **4** | Workflow Agent — authoring only | a published workflow passes `assert_faithful` | 1–2 w |
| **5** | One SoR adapter + drift detectors on a schedule | a real drift signal reaches the queue | 1 w |
| **6** | Development Agent | a generated package compiles and its evals bite | 1–2 w |
| **7** | Gateway, Memory, Evaluations, observability dashboards | production-shaped | 1–2 w |
| **8** | Training pipeline | first SFT run | see `docs/06-training.md` |

**Phase 1 before phase 3 is the important ordering.** The gate service is the
platform's actual product. If humans will not use the review queue, no amount of
agent quality matters, and you want to find that out in week two with a hand-written
proposal — not in month three after building three agents that feed a queue nobody
opens.

---

## 2. Database

**Aurora PostgreSQL 16.8+** (or RDS PostgreSQL 17.1+). **pgvector ≥ 0.8.0 is a hard
requirement** — `db/001` refuses to install below it. Below 0.8.0, filtered ANN
queries return incomplete results silently, which is a correctness bug that
presents as "the graph doesn't know that."

```sql
SELECT extversion FROM pg_extension WHERE extname = 'vector';  -- must be >= 0.8.0
```

Apache AGE is not available on RDS or Aurora and this schema does not use it. If
someone proposes migrating to it, the cost is self-managing Postgres on EC2/EKS.

### Sizing

Memory-optimised instance classes. The HNSW graph must fit in RAM or query latency
falls off a cliff.

Worked example, 100k nodes at 1024 dims with `halfvec` indexes:

```
raw vectors:  100,000 x 1024 x 2 bytes  =  205 MB
HNSW index:   ~4-5x raw                 =  ~1 GB per index
7 partial node indexes + 2 edge + 1 chunk, mostly overlapping subsets
                                        =  budget ~3-4 GB of index
plus shared_buffers, work_mem, connections
```

`db.r6g.xlarge` (32 GB) is comfortable for several engagements at this scale.

**Aurora Serverless v2 needs care.** ACU-driven memory scaling can evict
`shared_buffers` on scale-down, cold-starting a large resident HNSW graph; the
planner then costs a sequential scan as cheaper and stops using the index. Set the
**minimum ACU** high enough that the graph never falls out of memory rather than
relying on the low end of the range. (This is informed inference from Serverless v2
scaling behaviour plus community reports, not an AWS-published guarantee — see
`docs/99-sources.md`.)

### Index builds

```sql
SET maintenance_work_mem = '2GB';            -- ~4-5x raw vector bytes
SET max_parallel_maintenance_workers = 7;    -- parallel HNSW build
```

If the in-progress graph does not fit in `maintenance_work_mem`, pgvector logs a
notice and spills, and the build slows by an order of magnitude. `REINDEX` after
more than ~30% row churn — HNSW graph quality degrades with heavy updates.

### Migrations

`db/rebuild.sh` drops, recreates, applies `0*.sql` in order, and runs the smoke
test. That is the CI gate: **migrations must apply cleanly in order on an empty
database, and all 16 smoke tests must pass.** Production uses the same files
forward-only (no drop) via your migration runner of choice.

---

## 3. AgentCore Runtime

### Two artifact shapes

```python
# Container: ARM64 MANDATORY.
agentRuntimeArtifact={'containerConfiguration': {'containerUri': f'{ecr}:{tag}'}}

# CodeZip: no architecture constraint, no Docker, no ECR.
agentRuntimeArtifact={'codeConfiguration': {
    'code': {'s3': {'bucket': 'fde-agent-code', 'prefix': 'engagement/v1/'}},
    'runtime': 'PYTHON_3_12',
    'entryPoint': ['agent.py'],
}}
```

Use **CodeZip in dev** — the iteration loop is minutes instead of a buildx cycle.
Use **containers in prod**, where you want the dependency set pinned by image
digest.

For containers:

```bash
docker buildx create --use
docker buildx build --platform linux/arm64 \
  -t "$ACCT.dkr.ecr.$REGION.amazonaws.com/fde-engagement:$TAG" --push .
```

Forgetting `--platform linux/arm64` produces an image that pushes fine and fails at
runtime.

### Registration

```python
control = boto3.client('bedrock-agentcore-control', region_name=REGION)
control.create_agent_runtime(
    agentRuntimeName='fde_engagement',
    agentRuntimeArtifact={...},
    networkConfiguration={'networkMode': 'PUBLIC'},
    roleArn=f'arn:aws:iam::{ACCT}:role/FdeAgentRuntimeRole',
    lifecycleConfiguration={'idleRuntimeSessionTimeout': 900, 'maxLifetime': 28800},
)
```

### The limits that shape design

| Limit | Value | Consequence |
|---|---|---|
| Max session lifetime | **8 hours** | overnight human approvals cannot be a live session wait |
| Idle timeout | **15 min** | `/ping` must stay responsive; use `add_async_task` |
| Sync request timeout | 15 min | long tasks must stream or go async |
| Streaming max | 60 min | |
| Payload | 100 MB req/resp, 10 MB chunk | large transcripts go via S3, not inline |
| Hardware | 2 vCPU / 8 GB per session | no local model inference |
| Concurrency | 5,000 sessions (us-east/west), 2,500 elsewhere | |
| `InvokeAgentRuntime` | 200 TPS per agent | |
| Session storage | 1 GB | |

**Session isolation is 1:1 with a microVM** — dedicated compute, memory, and
filesystem per session, sanitised on termination. That is a real security property
worth stating to whoever asks about tenant isolation.

Session IDs must be ≥33 characters and reused across a whole conversation. Use the
same value for `trn.trace_session.session_id` so CloudWatch spans and training
traces join without a correlation table.

---

## 4. IAM

### Trust policy

The confused-deputy conditions are not optional.

```json
{
  "Version": "2012-10-17",
  "Statement": [{
    "Effect": "Allow",
    "Principal": {"Service": "bedrock-agentcore.amazonaws.com"},
    "Action": "sts:AssumeRole",
    "Condition": {
      "StringEquals": {"aws:SourceAccount": "${ACCOUNT}"},
      "ArnLike": {"aws:SourceArn": "arn:aws:bedrock-agentcore:${REGION}:${ACCOUNT}:*"}
    }
  }]
}
```

### Permissions

`agents/deploy/iam/runtime-permissions-policy.json`. Required:

- `ecr:BatchGetImage`, `ecr:GetDownloadUrlForLayer`, `ecr:GetAuthorizationToken`
- `logs:*` scoped to `/aws/bedrock-agentcore/runtimes/*`
- `xray:PutTraceSegments`, `PutTelemetryRecords`, `GetSamplingRules`, `GetSamplingTargets`
- `cloudwatch:PutMetricData` **conditioned on namespace `bedrock-agentcore`**
- `bedrock-agentcore:GetWorkloadAccessToken`, `...ForJWT`, `...ForUserId`
- `bedrock:InvokeModel`, `InvokeModelWithResponseStream`
- `secretsmanager:GetSecretValue` on the DB secret only
- `rds-db:connect` for IAM auth

Development Agent additionally needs the Code Interpreter actions
(`arn:aws:bedrock-agentcore:${REGION}:${ACCOUNT}:code-interpreter/*`).

### Database roles are the real boundary

IAM controls what the *runtime* can reach. Postgres roles control what the *agent*
can do, and that is where the platform's invariant lives:

| Role | Can | Cannot |
|---|---|---|
| `fde_agent` | read `kg.*`, create/submit proposals, draft workflows, triage drift, write traces | write `kg.node`/`kg.edge`/`kg.commit`; merge; publish a workflow; set its own training label |
| `fde_gate_service` | `EXECUTE hitl.merge_proposal`; write labels | — |
| `fde_ingest` | write observations and embeddings | touch `hitl.*` |
| `fde_prodops` | run workflows, decide gates, triage | write the graph directly |
| `fde_training` | read traces, write duels and failure labels | write production |
| `fde_rl_rollout` | read `kg.*`, write its own traces | reach `hitl.*` at all; UPDATE `trace_session` |

Every MCP tool call runs inside a transaction with `SET LOCAL ROLE fde_agent` and
`SET LOCAL statement_timeout = '20s'`. The connecting user is owner-adjacent; the
role switch is what constrains it.

---

## 5. MCP server

Two shapes, and the choice is not obvious.

### In-runtime (AgentCore Runtime, `protocol=MCP`)

Streamable HTTP on port 8080 at `/mcp`, `Mcp-Session-Id` header.

Right when: the agent is the only consumer, latency matters, and you want the MCP
server's lifecycle tied to the agent's.

### Behind AgentCore Gateway (`mcpServer` target)

```python
control.create_gateway(
    name='fde-kg-gateway',
    roleArn=GATEWAY_ROLE,
    protocolType='MCP',
    protocolConfiguration={'mcp': {
        'searchType': 'SEMANTIC',            # -> x_amz_bedrock_agentcore_search
        'supportedVersions': ['2025-03-26'],
        'sessionConfiguration': {'sessionTimeoutInSeconds': 900},
    }},
    authorizerType='CUSTOM_JWT',
    authorizerConfiguration={'customJWTAuthorizer': {
        'discoveryUrl': f'https://cognito-idp.{REGION}.amazonaws.com/{POOL}/.well-known/openid-configuration',
        'allowedAudience': [CLIENT_ID],
        'allowedClients': [CLIENT_ID],
    }},
)
```

Endpoint: `https://{gateway-id}.gateway.bedrock-agentcore.{region}.amazonaws.com/mcp`.
Streamable HTTP only. `Authorization: Bearer <token>`.

Right when: multiple agents share the tool surface, you want JWT-scoped access per
caller, or the tool count grows enough that semantic tool search earns its keep.
Limits: 100 targets/gateway, 1,000 tools/target, 6 MB tool payload, 15-minute
gateway timeout.

**Recommendation:** start in-runtime, move to Gateway at the point where a second
agent needs the same tools. Gateway costs $0.005/1k invocations plus $0.02/100 tools
indexed/month — trivial, but the added auth hop is real operational surface you do
not need on day one.

---

## 6. Human-in-the-loop deployment

Covered in depth in `docs/07-hitl-gates.md` §6. The deployment-relevant summary:

- Use `app.add_async_task()` / `app.complete_async_task()` with a non-blocking poll.
- **Do not** use `arn:aws:states:::bedrockagentcore:invokeHarness` for approval
  waits. Request-Response only; `.sync` and `waitForTaskToken` are explicitly not
  supported; 15-minute cap regardless of `TimeoutSeconds`.
- Approvals that may exceed 8 hours persist to `wf.run_step` with
  `status='awaiting_human'`, end the session, and resume in a fresh one.

---

## 7. Scheduled work

| Job | Cadence | Mechanism |
|---|---|---|
| Drift scan | 6 h | EventBridge → Lambda → `invoke_agent_runtime(fde_workflow, monitor_drift)` |
| Embedder worker | continuous | ECS service draining `kg.embed_queue` |
| SoR adapter poll | per `sor.adapter.poll_cron` | EventBridge → Lambda |
| Proposal expiry sweep | 1 h | Lambda → mark expired, notify |
| Judge calibration | weekly | SageMaker Processing → `trn.judge_kappa` |
| `REINDEX` check | monthly | Lambda → alert if churn > 30% |

Six hours for drift is a starting point. Tune from `sor.drift_signal` arrival rate:
if consecutive scans mostly produce `occurrences` increments rather than new
signals, lengthen it.

---

## 8. Observability

AgentCore emits OTEL-format telemetry to CloudWatch. **Enable CloudWatch Transaction
Search once** or the GenAI dashboard has no data.

Automatic: metrics for every resource type — session count, latency, duration, token
usage, error rates. Spans and logs are automatic for Payments and Policy resources;
Memory requires explicit enablement; **your own agent logic requires you to
instrument it** with the OTEL SDK. `agents/common/tracing.py` does this.

Model: Session → Trace → Span. `session_id` is the join key to `trn.trace_session`.

### Dashboard that matters

| Panel | Source | Watch for |
|---|---|---|
| Proposals by status | `hitl.proposal` | `submitted` growing = review bottleneck |
| **Median `review_seconds` by gate kind** | `hitl.gate_decision` | flat and small = rubber-stamping |
| Edit rate | `proposal_item.item_status` | ~0% = not reading; >60% = agent miscalibrated |
| Expiry rate | `proposal.expires_at` | >5% = SLA or staffing problem |
| Open drift by severity | `sor.drift_signal` | monotonic growth = thresholds too low |
| Workflows behind head | `wf.workflow` vs `kg.commit` | staleness accumulating |
| Retrieval p50/p95 | trace `retrieval` JSON | p95 spike = index not in memory |
| Ungrounded-claim rate | `trace_step.grounded` | rising = hallucination regression |

The four HITL-health numbers in `docs/07-hitl-gates.md` §8 are the ones to alarm on.
Everything else is diagnostic.

---

## 9. Cost

AgentCore Runtime bills on **active consumption** — $0.0895/vCPU-hour,
$0.00945/GB-hour, 1-second minimum, and CPU is not charged during pure I/O wait with
no background processes. That last clause matters: a session parked on a human
approval with an async task registered is mostly not burning CPU.

Rough monthly, one active engagement, 200 agent sessions averaging 8 minutes at
1 vCPU / 2 GB:

| Component | Estimate |
|---|---|
| Runtime compute | ~$3 |
| Bedrock model inference (Claude, ~500k tok/session) | **dominant — model choice is the cost decision** |
| Embeddings (Titan v2) | < $1 at these volumes |
| Gateway (100k invocations) | ~$0.50 |
| Memory (10k events, 5k records) | ~$7 |
| Aurora `db.r6g.xlarge` | ~$380 |
| **Aurora dominates at small scale; model inference dominates at large scale.** | |

Do not optimise runtime compute. Optimise model selection and retrieval efficiency —
`r_cost` in the RL reward exists partly because a policy that pulls 400 nodes to
answer a 2-node question costs real money at volume.

---

## 10. CI/CD

```yaml
on: [pull_request]
jobs:
  db:
    # postgres:16 service + pgvector 0.8.2 built with OPTFLAGS=""
    steps:
      - run: ./db/rebuild.sh fde_ci        # migrations + 16 smoke tests
  code:
    steps:
      - run: python -m pytest mcp/test_server.py -q
      - run: python -m pytest training/test_grpo_rewards.py -q
      - run: python -m py_compile $(git ls-files '*.py')
  agents:
    steps:
      - run: docker buildx build --platform linux/arm64 agents/engagement
      # ... workflow, development
  deploy:            # main only, manual approval
    steps:
      - run: python agents/deploy/create_runtimes.py --tag ${{ github.sha }}
      - run: python -m pytest tests/smoke_deployed.py
```

**Build pgvector with `OPTFLAGS=""`.** The default `-march=native` produces a binary
tuned to the build host's CPU. On a heterogeneous fleet — or a CI runner that
migrates — Postgres crashes with **SIGILL (signal 4)** the first time an HNSW query
executes an unsupported instruction. This repo hit it: the smoke test passed, the
host changed, and the identical test then killed the server mid-query. It reads like
data corruption and it is not.

### Deployment strategy

Runtime **versions and endpoints** give you blue/green: deploy a new version, point
a `canary` endpoint at it, invoke with `qualifier='canary'`, compare
`trn.trace_session` outcomes by `agent_qualifier`, promote. 1,000 versions per agent
and 10 endpoints per agent is plenty.

---

## 11. Multi-tenancy

`engagement_id` is on every row of every table and in every function signature. It
is the hard seam.

For stronger isolation, add row-level security:

```sql
ALTER TABLE kg.node ENABLE ROW LEVEL SECURITY;
CREATE POLICY node_engagement_isolation ON kg.node
  USING (engagement_id = current_setting('fde.engagement_id')::uuid);
```

with the MCP server setting `SET LOCAL fde.engagement_id` alongside `SET LOCAL ROLE`.
Not shipped by default because it adds a failure mode (a forgotten `SET LOCAL`
returns zero rows rather than erroring, which is confusing to debug) — turn it on
when you have more than one client's data in one cluster, and add a smoke test that
asserts cross-engagement reads return nothing.

Separate Aurora clusters per client is the stronger answer where contracts demand it.

---

## 12. Pre-production checklist

- [ ] pgvector ≥ 0.8.0 verified in the target environment
- [ ] pgvector built with `OPTFLAGS=""` (or a distro package)
- [ ] `db/rebuild.sh` green in CI on a clean database
- [ ] All 16 smoke tests passing, including 2/3/4 (the fail-closed tests)
- [ ] `hitl.merge_proposal` `EXECUTE` revoked from PUBLIC; granted only to the gate service
- [ ] `fde_agent` verified unable to write `kg.node` (test it, do not assume)
- [ ] Gate policy reviewed with the actual reviewers who will clear the gates
- [ ] Reviewer authorities granted per engagement, with `granted_by` recorded
- [ ] CloudWatch Transaction Search enabled
- [ ] `review_seconds` visible on the prod-ops dashboard
- [ ] Runtime execution role has the `SourceAccount` / `SourceArn` conditions
- [ ] Containers built ARM64
- [ ] Session IDs ≥33 chars and stable across a conversation
- [ ] Human steps use `add_async_task` with a non-blocking poll
- [ ] `maintenance_work_mem` sized; index build times measured, not assumed
- [ ] DB credentials in Secrets Manager or IAM auth; nothing in env vars
- [ ] Bedrock Guardrail attached for PII and prompt injection
- [ ] Backup/PITR configured on Aurora
- [ ] A runbook entry for "the drift queue is full of noise"
