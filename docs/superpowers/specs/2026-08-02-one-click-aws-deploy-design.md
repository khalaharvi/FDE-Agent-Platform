# One-click AWS deploy (Launch Stack) — design

**Date:** 2026-08-02 · **Status:** implemented on feat/one-click-deploy (pre-live-validation)
**Sub-project 2 of 2** (follows the multi-provider spec, which shipped the provider seam this stack parameterizes). Branch `feat/one-click-deploy`, stacked on `feat/multi-provider-llm`; rebase onto `main` after PR #1 merges.

## Problem

The repo has zero IaC. Three hand-run CLIs (`fde-agents-deploy`, `fde-gate-deploy`, `fde-sor deploy`) create application-layer resources only; the entire account substrate (VPC, Aurora, 7+ IAM roles, Cognito, ECR, S3, secrets) is undocumented manual work, and two services — the KG MCP server and the embedder worker — have **no deploy path at all** (the AgentCore Gateway subcommand is unrunnable without an MCP endpoint). Nothing has ever run against live AWS. A newcomer cannot get from the README to a running platform without days of assembly.

## Goal

A **Launch Stack button** in the README: one click → CloudFormation console → a handful of parameters → a running core loop (ingest → propose → review console → merge → search), **live-validated in a real AWS account before the button ships**. Clean teardown via stack delete.

## Decisions (user-confirmed)

1. **Vehicle:** true README Launch-Stack button (hosted CloudFormation template). A `fde-deploy` CLI wrapper is explicitly deferred — cheap later, same synth source.
2. **Validation:** the user will live-test in a real AWS account; the button does not enter the README until launch succeeds end-to-end. Honesty labels then flip to "live-validated on <date>, region X" — the repo's first.
3. **Images:** prebuilt ARM64 images on **ECR Public**, pushed by a new tag-triggered release workflow; template pins the release tag. (Also retires CI's `push: false` dead end and the deploy job's latent `--update` bug.)
4. **Scope:** **core-loop stack** — everything needed for out-of-the-gate value — plus a documented path for operationalizing the full platform (SoR adapters, training, hardening) afterward. SoR Lambdas/SQS and the RFT grader are not in the stack.
5. **Authoring:** **CDK in Python**, synthesized to the hosted template. Native AgentCore support confirmed: `AWS::BedrockAgentCore::Runtime/Gateway/GatewayTarget/Memory` exist and `aws-cdk-lib.aws_bedrockagentcore` L2 constructs are stable — no custom resources for the agent layer.

## Design

### 1. CDK app (`infra/cdk/`)

An **isolated** uv project (own `pyproject.toml`, own lock, own venv — deliberately NOT a workspace member; the `train`-extra venv incident is the precedent). One root `FdePlatformStack` — a single template URL keeps the button simple — internally decomposed into constructs with one responsibility each:

`Network` · `Database` · `Identity` · `GateService` · `McpService` · `Embedder` · `Agents` · `Outputs`

CI gains an offline job: `cdk synth` + `cfn-lint` on every PR touching `infra/cdk/` (no AWS credentials needed).

### 2. Parameters and outputs (the click experience)

| Parameter | Default | Notes |
|---|---|---|
| `DeployTier` | `demo` | `demo` = Aurora Serverless v2 with a documented min-ACU floor (HNSW must stay inside shared_buffers — docs/09); `production` = `db.r6g.xlarge` |
| `ModelProvider` | `bedrock` | the multi-provider seam: bedrock / anthropic / openai / gemini / openai-compat |
| `ModelId` | (blank) | required for non-bedrock; validated by the stack |
| `ProviderApiKey` | (blank) | **NoEcho** → Secrets Manager → runtime env (`ANTHROPIC_API_KEY` etc.); empty is valid for bedrock |
| `CompatBaseUrl` | (blank) | openai-compat only |
| `EmbedProvider` / `EmbedModelId` | `bedrock` / titan-v2 | same seam, embeddings side |
| `AdminEmail` | (required) | seeds the Cognito user AND the gate principal so the review console works on first login |
| `ReleaseTag` | pinned per release | selects the ECR Public image set + artifact zips |

Outputs: review-console URL, Cognito hosted-UI login URL, API endpoint, MCP endpoint, link to the first-steps doc.

### 3. Substrate

- **VPC:** 2-AZ, private subnets, single NAT (demo-cost posture).
- **Aurora PostgreSQL** pinned at the pgvector ≥ 0.8.0 engine floor (docs/09: Aurora PG 16.8+; `db/001` refuses below 0.8). Snapshot-on-delete; deletion protection only in `production` tier.
- **Secrets Manager:** DB credentials in the standard RDS-rotation JSON shape (`fde_mcp.db._dsn_from_secrets_manager` already reads exactly that), provider API key secret.
- **Cognito:** user pool + three clients — API Gateway JWT authorizer, Gateway CUSTOM_JWT (discovery URL), M2M client-credentials for `FDE_MCP_TOKEN`.
- **IAM:** the 7 roles rendered from the repo's existing checked-in policy JSON templates (`deploy/iam/*.json` in fde-agents/fde-gate/fde-sor) — those `${AWS_ACCOUNT_ID}`-placeholder files finally consumed as designed, in CDK.
- **Migrations:** one Lambda-backed custom resource (zip built like the gate's) applying `db/001`–`015` forward-only + creating the per-environment login users (`db/010` creates NOLOGIN roles only) + seeding the `AdminEmail` principal. Idempotent via a migration-ledger table; runs on create and update.

### 4. Services

- **Gate:** Lambda from the `fde-gate-deploy package` zip (built by the release pipeline, fetched from the assets bucket), HTTP API v2 with JWT authorizer + unauthenticated `/healthz`, and the two EventBridge rules (`fde-gate-tick` 1 min, `fde-gate-expiry` 1 h) — the same shapes the existing CLI creates, now declared in CDK.
- **MCP server:** ARM64 Fargate service (image from ECR Public), streamable HTTP `:8080/mcp`, behind an **internal ALB**; this endpoint is what makes the AgentCore Gateway finally provisionable.
- **Embedder:** sibling Fargate service in the same task family running `fde-embedder`, as `fde_ingest`.
- **Agents:** three `CfnRuntime`/L2 AgentCore runtimes (container mode) + Gateway (MCP target → the ALB endpoint, CUSTOM_JWT via Cognito) + Memory with the three strategies — all native CDK, mirroring the env wiring `fde-agents-deploy` bakes today (`FDE_MODEL_ID` recorded per runtime for audit parity; provider env from §2's parameters).
- **Verify at implementation:** whether AgentCore runtimes require in-account private ECR; if so, an **ECR pull-through cache rule** from the public gallery satisfies it with no CodeBuild.

### 5. Release pipeline (the button's supply chain)

New `release.yml` (tag-triggered): build + push the 5 ARM64 images to ECR Public; build the gate zip + migration-runner zip → assets bucket; `cdk synth` → hosted template in the assets bucket; README button URL pinned to the release tag. The existing CI `deploy` job is corrected/repointed in the process (its `runtimes --update` invocation is rejected by `runtimes.py` today — a latent bug that has never fired because the secrets gate skips it).

### 6. Validation, teardown, docs

- **Live-test protocol:** launch in the user's account → run the docs/12 provider flow against the deployed endpoints (Cognito login → review console → an agent task through the Gateway → merge → `kg_search`) → record actual costs → only then add the button to the README, labeled live-validated with date/region.
- **Teardown:** `aws cloudformation delete-stack` (or console) removes everything; demo tier leaves a final DB snapshot.
- **Docs:** new `docs/13-launch-stack.md` — what the button deploys, the parameters, first steps after launch, real observed costs, and **"operationalizing the full platform"**: when and how to add SoR adapters (`fde-sor deploy` / infra/k8s), the training pipeline, and the docs/10 hardening checklist.

## Non-goals (v1)

SoR/RFT resources in the stack; multi-region/DR; custom domains + ACM; a CLI wrapper; AWS Marketplace listing; Bedrock Guardrails provisioning (documented as a follow-up hardening step, per docs/09's checklist).

## Risks, honestly

- **First-ever live AWS run.** Every "written against verified API shapes" claim in the repo gets tested at once; expect the live-test cycle to surface real bugs (that is its purpose — budget iteration time, not a single attempt).
- **AgentCore CFN/L2 maturity:** the constructs are new-generation; if an L2 gap appears, fall back to L1 `Cfn*` types (still no custom resources).
- **Aurora cold-start + ACU floor:** demo tier must document the HNSW/shared_buffers trade honestly rather than default to a config that quietly degrades recall.
- **ECR Public quotas/availability** for anonymous pulls at launch time — mitigated by the pull-through-cache pattern if needed.
