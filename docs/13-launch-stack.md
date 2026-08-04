# Launch stack

What the README's Launch-Stack button deploys, the parameters you fill in,
what to do the moment it finishes, and what it does not do for you.

**Honesty label:** the stack (`infra/cdk/`) is written against verified CDK
and AgentCore CloudFormation resource-provider schemas, synth-tested,
`cfn-lint`-clean, and unit-tested — it has **not been live-validated against
a real AWS account.** The README's Launch-Stack button does not exist yet;
per the platform's own AWS honesty rule (`CLAUDE.md`), it appears only after
a real launch succeeds end to end and this document's cost/timing numbers
are replaced with observed ones. Until then, treat everything below as
"should work, has not been watched to work."

---

## 1. What the button deploys

One CloudFormation stack (`FdePlatformStack`, `infra/cdk/fde_cdk/stack.py`),
one template URL, nine single-responsibility constructs plus a plain
`add_outputs` function (not a tenth `Construct` — see `outputs.py`'s own
docstring for why: clean, unhashed `CfnOutput` names on the CloudFormation
console), built/called in this order: `Network → Database → Identity →
IamRoles → Migrations → Services → Agents → GateService → OpsLayer →
Outputs`.

**Network** (`network.py`) — one VPC, 2 AZs, one NAT gateway (a deliberate
single point of failure for this demo-cost posture), public + private-with-
egress subnets only.

**Database** (`database.py`) — one Aurora PostgreSQL 16.8 cluster (pgvector),
`fde` as the default database. `DeployTier=demo` provisions a Serverless v2
writer (2–8 ACU); `production` swaps in `db.r6g.xlarge` and turns on
deletion protection — both branches are baked into the template as
`Fn::If`, selected at launch, not at synth time. Both tiers get: 7-day
automated backup retention, snapshot-on-delete (the cluster's data outlives
the stack), and the RDS Data API enabled (`enable_data_api=True` — this is
the break-glass query path, §5).

**Identity** (`identity.py`) — one Cognito user pool, self-signup off. Three
app clients: `console_client` (hosted-UI, authorization-code grant, for the
review console), `api_client` (no OAuth flows of its own — exists to give
the gate API's JWT authorizer a stable audience), `m2m_client`
(client-credentials, scoped to `gateway/invoke` — backs the `fde-gateway-m2m`
AgentCore Identity credential provider `agents.py` provisions for a future
Gateway wire-up; no deployed runtime calls it in v1 — see §6). One user is
seeded from `AdminEmail` at launch. The user pool's removal policy is
`RETAIN`: deleting the stack does not delete your accounts.

**IamRoles** (`iam_roles.py`) — five execution roles. `runtime_role` (shared
by all three AgentCore runtimes — see the hardening note in §7) and
`gate_role` are rendered directly from the repo's own checked-in IAM policy
JSON (`packages/fde-agents/.../deploy/iam/*.json`,
`packages/fde-gate/.../deploy/iam/*.json`); `migration_role`,
`gateway_service_role`, and `memory_role` have no repo template and are
derived from the exact API calls their consumers make.

**Migrations** (`migrations.py` + `lambdas/migration_runner/`) — a
VPC-attached Lambda invoked once per deploy via a CloudFormation custom
resource (no CDK asset framework — see `infra/cdk/README.md`'s
bootstrap-free contract). It applies `db/001`–`db/015` forward-only against
a ledger table (`ops.applied_migration`), mints three least-privilege
Postgres login users and their Secrets Manager credentials (`fde/db/agent`,
`fde/db/gate`, `fde/db/ingest` — see `db/010`'s NOLOGIN group-role design),
and seeds `hitl.reviewer` with `AdminEmail` (see the break-glass
reconciliation this requires, §5). The same Lambda also answers OpsLayer's
embed-queue-depth probe (§4).

**Services** (`services.py`) — one ECS cluster; `fde-mcp` (ARM64 Fargate,
streamable HTTP on `:8080/mcp`) behind an **internal** ALB (health-checked
on `/mcp` itself, `200-499` treated as healthy — see that module's own
docstring for why, and §7's `/healthz` hardening note); `fde-embedder`
(same image, `command=["fde-embedder"]`, no load balancer — it drains
`kg.embed_queue`). Neither is reachable from outside the VPC.

**Agents** (`agents.py`) — an ECR-Public pull-through cache rule (AgentCore
Runtime requires a *private*-registry image, unlike Fargate); one AgentCore
Gateway (MCP protocol, `SEMANTIC` search, `CUSTOM_JWT` against the Cognito
pool) — **provisioned but unwired**: no `GatewayTarget` points it at the MCP
ALB (see §6); one AgentCore Memory resource (three always-on strategies:
semantic, summary, user-preference; 90-day event expiry); three AgentCore
Runtimes (Engagement, Workflow, Development — `VPC` network mode, one shared
security group with a private route to Aurora, container image resolved
through the pull-through cache); one AgentCore Identity OAuth2 credential
provider (`fde-gateway-m2m`, wired against `m2m_client`'s Cognito discovery
URL/id/secret) — provisioned for a future v2 Gateway wire-up, not currently
called by any runtime. Every runtime talks to the FDE knowledge-graph MCP
server over its own in-container stdio subprocess, not the Gateway — see §6
for why, and for the no-public-MCP-gateway decision behind it.

**GateService** (`gate.py`) — the `fde-gate-service` Lambda (VPC-attached,
python3.12/arm64), an `apigatewayv2.HttpApi` (JWT-authorized `ANY
/{proxy+}`, unauthenticated `GET /healthz`), and the two EventBridge
schedules the CLI-driven deploy also creates (`fde-gate-tick` every minute,
`fde-gate-expiry` every hour), both wired to a shared DLQ (`fde-ops-dlq`).
This is the review console and the merge API.

**OpsLayer** (`ops.py`) — the pluggable ops/alarm package. See §4.

**Outputs** — seven `CfnOutput`s printed the moment the stack finishes:
`ReviewConsoleUrl`, `CognitoLoginUrl`, `ApiEndpoint`, `McpEndpoint`
(internal-only, printed for VPC-internal debugging), `FirstStepsUrl` (a
link back to this document), `OpsTopicArn`, `OpsDashboardUrl`.

---

## 2. Parameters

Every one of these is a `cdk.CfnParameter` in `infra/cdk/fde_cdk/params.py`
— that module is ground truth; this table transcribes it, not the other
way around.

| Parameter | Default | Notes |
|---|---|---|
| `DeployTier` | `demo` | `demo` = Aurora Serverless v2 (2–8 ACU floor, sized so the HNSW index survives a scale-down — docs/09 §2); `production` = `db.r6g.xlarge` + deletion protection |
| `ModelProvider` | `bedrock` | `bedrock` / `anthropic` / `openai` / `gemini` / `openai-compat` — the multi-provider seam (docs/12) |
| `ModelId` | *(blank)* | Required for non-bedrock providers; ignored under `bedrock` (per-agent defaults apply — enforced by the stack, not just documented) |
| `ProviderApiKey` | *(blank)* | **NoEcho.** Mirrored into **one** Secrets Manager secret, read by every consumer via a `{{resolve:secretsmanager:...}}` dynamic reference — never baked into the template in plaintext. Blank is valid for `bedrock`. **This one secret feeds both `FDE_MODEL_API_KEY` and `FDE_EMBED_API_KEY`** — see the one-vendor-key caveat below |
| `CompatBaseUrl` | *(blank)* | `openai-compat` only. Shared between the model provider and the embedding provider (v1 has one base-URL parameter, not two) |
| `EmbedProvider` | `bedrock` | `bedrock` / `openai` / `gemini` / `openai-compat` — the *provider choice* is independent of `ModelProvider`, but see the one-vendor-key caveat below: `ProviderApiKey` is the one secret both providers' API-key env vars read from |
| `EmbedModelId` | `amazon.titan-embed-text-v2:0` | |
| `AdminEmail` | *(required, no default)* | Seeds the Cognito admin user **and** the `hitl.reviewer` row the review console needs on first login. Read §5 before you assume the console works immediately after login |
| `ReleaseTag` | pinned per release | Selects the ECR Public image set and the two artifact zips (gate Lambda, migration runner) this launch uses |
| `AssetsBucket` | *(blank → region default)* | Override the S3 bucket the Lambda code zips are fetched from; blank uses `AssetsRegionMap`'s `us-east-1` default |
| `OpsMode` | `email` | `email` / `topic-only` / `off` — see §4 |
| `OpsAlertEmail` | *(blank → falls back to `AdminEmail`)* | Where the ops SNS topic's default email subscription and the budget notification go, when `OpsMode=email` |
| `MonthlyBudgetUsd` | `300` | AWS Budgets monthly threshold (80% actual-spend trigger). `0` disables the `AWS::Budgets::Budget` resource entirely, independent of `OpsMode` |

### 2.1 The one-vendor-key limitation

`params.py` has exactly one API-key parameter, `ProviderApiKey` — there is
no separate `EmbedApiKey`. `gate.py` and `services.py` both feed that same
secret into `FDE_MODEL_API_KEY` (the model provider's key) **and**
`FDE_EMBED_API_KEY` (the embedding provider's key), via the identical
`dynamic_secret_env_value` helper. That means:

- **Same-vendor pairings work:** `ModelProvider=openai` +
  `EmbedProvider=openai` (one OpenAI key covers both), or `bedrock`
  for either/both (no key needed at all).
- **A cross-vendor pairing that needs two *different* keys does not work
  out of the box** — e.g. `ModelProvider=anthropic` (chat) +
  `EmbedProvider=openai` (embeddings, since Anthropic has no embeddings
  API — docs/12 §1) needs an `ANTHROPIC_API_KEY` *and* an
  `OPENAI_API_KEY`, but this stack's launch parameters can only carry one
  secret value into both env vars. Launching this pairing as-is will
  authenticate one of the two providers with the wrong vendor's key and
  fail.
- **Workaround today (post-launch, manual):** after the stack is up, either
  edit the MCP/embedder Fargate task definitions' environment directly (ECS
  console, or a follow-up `cdk deploy` from a local checkout with a second
  secret wired in by hand) to point `FDE_EMBED_API_KEY` at a second,
  manually created Secrets Manager secret, or create that second secret
  yourself and reference its ARN in place of the shared one.
- **The real fix is v2 scope**, not something to hand-patch into this
  launch stack: a second `EmbedApiKey` `CfnParameter` + a second Secrets
  Manager secret, mirroring `ProviderApiKey`'s own shape. Tracked in §8's
  hardening list.

---

## 3. First steps after launch

**Known gap, fixed during live-test iteration: the zero-config browser
console does not work yet.** `ReviewConsoleUrl` is the gate API's `/ui`
route, behind the SAME JWT authorizer as every other `/{proxy+}` route — a
bare browser navigation carries no `Authorization` header, and
`packages/fde-gate` has no server-side OAuth code-exchange route today that
would turn a Cognito hosted-UI redirect into an attached bearer token.
Clicking `CognitoLoginUrl` gets you a real sign-in and a redirect back to
`ReviewConsoleUrl?code=...`, and then a 401 — nothing in this stack sets a
session or attaches the token for you yet. That real fix is a
`packages/fde-gate` change (a server-side callback route), out of scope for
this CDK-only fix wave; it is fixed during the live-test iteration. Until
then, use the scripted path below to get a bearer token and drive the gate
API directly.

1. Open `CognitoLoginUrl` and sign in (check the `AdminEmail` inbox for
   Cognito's temporary-password email, set a real password). This confirms
   the seeded account works; it will NOT leave you inside a working console
   session — see the gap above.
2. Get a bearer ID token one of two ways (both authorizer-audience paths are
   now accepted — see this fix wave's `gate.py` change):
   - **Hosted-UI code exchange, using `console_client`** (has a secret):
     after step 1's redirect, copy the `code` query parameter from the
     browser's address bar (`ReviewConsoleUrl?code=...`), then exchange it
     directly against Cognito's own token endpoint:

     ```bash
     # one-time: find console_client's id + secret (stack Resources tab
     # names the logical id; DescribeUserPoolClient returns both)
     aws cognito-idp describe-user-pool-client \
       --user-pool-id <UserPoolId> --client-id <ConsoleClient id> \
       --query 'UserPoolClient.[ClientId,ClientSecret]'

     curl -s -X POST "https://<hosted-ui-domain>.auth.<region>.amazoncognito.com/oauth2/token" \
       -H "Content-Type: application/x-www-form-urlencoded" \
       -u "<console_client_id>:<console_client_secret>" \
       -d "grant_type=authorization_code&code=<code-from-the-redirect>&redirect_uri=<ApiEndpoint>/ui" \
       | jq -r .id_token
     ```
   - **Scripted SRP against `api_client`** (no hosted UI, no secret —
     `generate_secret=False`): `api_client` sets no `explicit_auth_flows`,
     so Cognito's own documented default applies (`ALLOW_REFRESH_TOKEN_AUTH`,
     `ALLOW_USER_SRP_AUTH`, `ALLOW_CUSTOM_AUTH` — verified directly against
     the CDK library's bundled CloudFormation property docs), so
     `USER_SRP_AUTH` is available. The AWS CLI does not implement the SRP
     challenge-response math itself — use an SRP-capable Cognito client
     library (e.g. Python's `pycognito`/`warrant`, or
     `amazon-cognito-identity-js`) authenticating `AdminEmail`/your password
     against `api_client`'s id. The resulting ID token's `aud` is
     `api_client`'s id.
3. `curl -H "Authorization: Bearer <id-token>" <ApiEndpoint>/...` for every
   gate API call (merge, workflow run, drift resolution, etc.) — this is
   the console's actual API surface today. Driving the interactive `/ui`
   pages the same way needs the header attached to every request a browser
   makes (a request-modifying extension, or a small local proxy) until the
   code-exchange route ships.
4. **Before you rely on any of this for anything:** do the principal
   reconciliation in §5.1. Skip this and every approval you attempt will be
   rejected — not because anything is broken, but because the platform's own
   fail-closed design (docs/07) requires an authorized reviewer identity to
   match exactly.
5. Run one agent task against a deployed runtime (e.g. the engagement flow
   described in docs/12 §3, invoking the `ApiEndpoint`-mediated
   `InvokeAgentRuntime` call rather than a local dev checkout) so a real
   proposal lands in the queue. Every deployed runtime talks to the FDE
   knowledge-graph MCP server over its own in-container stdio subprocess,
   not the Gateway — see §6 for why.
6. Approve it (via the bearer-token `curl` path above). Confirm the merged
   commit is visible.
7. Confirm `kg_search`/the console's search surface returns fused results —
   this is docs/09's own phase-2 "done when," now checked against a live
   stack instead of a local one.
8. If you left `OpsMode=email` (the default), **confirm the SNS subscription
   email** in the `OpsAlertEmail` (or, if blank, `AdminEmail`) inbox — AWS
   sends a "Subscription Confirmation" email for every new SNS subscription,
   and it does nothing (no alarms reach you) until someone clicks confirm.
   This is easy to miss on a first launch because nothing *looks* broken in
   the meantime.
9. If you plan to rely on the AgentCore GenAI observability dashboards,
   **enable CloudWatch Transaction Search once** in this account/region —
   the stack does not do this for you (it is an account-level setting, not a
   stack resource); without it the dashboards have no data (docs/09 §8).

---

## 4. Bring your own ops stack

`OpsLayer` (`infra/cdk/fde_cdk/ops.py`) is built to be replaced by whatever
health/alerting stack you already run. The design stance, set explicitly by
a prod-ops review during this feature's build (see the SDD plan's Task 7.5
amendment): **the SNS topic is the only load-bearing seam.** Everything else
this construct builds — alarms, dashboard, the default email subscription,
the budget — is convenience wired to that topic; nothing upstream of the
topic depends on anything downstream of it.

**`OpsMode` matrix:**

| `OpsMode` | SNS topic (`fde-ops`) | Default email subscription | 8 alarms | Dashboard | Budget |
|---|---|---|---|---|---|
| `email` (default) | yes | yes, to `OpsAlertEmail` or `AdminEmail` | yes | yes | yes, if `MonthlyBudgetUsd != 0` |
| `topic-only` | yes | **no** | yes | yes | yes, if `MonthlyBudgetUsd != 0` |
| `off` | no | no | no | no | no |

**Note on the Budget column:** the Budget's own threshold notification
(`MonthlyBudgetUsd`'s 80%-actual-spend alert, `ops.py`'s `CfnBudget`) always
emails `OpsAlertEmail`/`AdminEmail` **directly**, via AWS Budgets' own native
email mechanism — it never routes through the `fde-ops` SNS topic, in any
`OpsMode`. So the "Default email subscription: no" row for `topic-only`
describes the *alarm* path only; a launcher who set `MonthlyBudgetUsd != 0`
still gets budget-threshold emails with no SNS email subscription anywhere
in the stack.

Set `OpsMode=topic-only` if you already run Datadog, PagerDuty, or a SIEM:
subscribe your own integration directly to the `OpsTopicArn` output
(SQS/Lambda/HTTPS subscription — whatever your tool takes) and every alarm
this stack raises reaches it, with no default email in the way. Set
`OpsMode=off` only if you are bringing an entirely separate health/alarm
stack and do not want any of these resources to exist at all (no topic
either).

**The eight alarms** (all wired to the topic, all conditioned on
`OpsMode != off`; names, metrics, and thresholds are pinned exactly —
`infra/cdk/tests/test_ops.py` enforces this table byte-for-byte):

| Alarm | Metric | Threshold | Evaluation |
|---|---|---|---|
| `FdeGateErrors` | `AWS/Lambda` `Errors` (fde-gate-service) | ≥ 3 | 3 × 1 min |
| `FdeMigrationRunnerErrors` | `AWS/Lambda` `Errors` (migration runner) | ≥ 1 | 1 × 5 min |
| `FdeMcpUnhealthyHosts` | `AWS/ApplicationELB` `UnHealthyHostCount` | ≥ 1 | 3 × 1 min |
| `FdeMcpTarget5xx` | `AWS/ApplicationELB` `HTTPCode_Target_5XX_Count` | ≥ 5 | 1 × 5 min |
| `FdeDbAcuCeiling` | `AWS/RDS` `ServerlessDatabaseCapacity` | ≥ 7.5 ACU | 1 × 15 min |
| `FdeDbConnections` | `AWS/RDS` `DatabaseConnections` | ≥ 360 (80% of the 2-ACU-floor `max_connections` estimate — see `ops.py`'s own derivation) | 1 × 5 min |
| `FdeEmbedQueueBacklog` | `FDE/Platform` `EmbedQueueDepth` (custom metric, probed every 5 min by the migration Lambda) | ≥ 500 | 1 × 15 min |
| `FdeOpsDlqMessages` | `AWS/SQS` `ApproximateNumberOfMessagesVisible` (`fde-ops-dlq`) | ≥ 1 | 1 × 5 min |

None of these alarms set `TreatMissingData` explicitly (CloudWatch's default,
`missing`) — see §7's hardening note on the count-based alarms.

---

## 5. Break-glass operations

Everything here works with **only the AWS console** — no bastion host, no
VPN, no SSH key — because Aurora's Data API (`enable_data_api=True`,
`database.py`) and Cognito's admin API are both reachable over the AWS
control plane, not the VPC.

### 5.1 Principal reconciliation (do this on first login — required)

`hitl.reviewer.principal` is normally the reviewer's Cognito JWT `sub`
claim (`fde_gate/http.py` derives the acting principal from it). The
migration Lambda seeds the admin reviewer row using `AdminEmail` as the
principal instead — it has no way to know the Cognito-assigned `sub` at
migration time, since that value is only assigned when the seeded user is
created, and `AWS::Cognito::UserPoolUser` exposes no `Fn::GetAtt` for it.

**Until you fix this, every review-gate decision the admin tries to make
fails closed** — `hitl.gates_satisfied`'s reviewer-authority join has no row
matching the admin's real JWT `sub`, so the system correctly refuses to
trust an unauthorized principal, which is exactly the invariant CLAUDE.md
says must never be weakened. This is not a bug to route around; it is the
fail-closed design working as intended on a seed value that cannot be
perfect at synth time.

Fix it once, right after your first login, using the CloudFormation
console → RDS → your cluster → **Query Editor** (backed by the Data API —
no connection string needed, just the cluster and the master secret both
already registered in Secrets Manager):

1. Get your own `sub`: after signing in, decode the ID token (any JWT
   decoder, e.g. `jwt.io`, or `aws cognito-idp get-user` with your access
   token) and copy its `sub` claim.
2. In the Query Editor, run:

   ```sql
   UPDATE hitl.reviewer
   SET principal = '<your-cognito-sub>'
   WHERE email = '<AdminEmail-you-launched-with>';
   ```

3. Confirm: attempt one approval in the review console. If it still fails
   closed, re-check the `sub` you copied — a stale ID token (from before a
   password reset, for instance) carries the same `sub`, so this is rarely
   the cause, but a copy/paste error is common.

### 5.2 Embed-queue probe (manual)

The same Query Editor answers "is the embedder keeping up" directly,
independent of the `FdeEmbedQueueBacklog` alarm:

```sql
SELECT count(*) FROM kg.embed_queue WHERE completed_at IS NULL;
```

A number that keeps growing across repeated checks means the `fde-embedder`
Fargate task is not draining the queue — check its own CloudWatch log
group and the `FdeOpsDlqMessages`/dashboard panels in §7's escalation
mapping before assuming it is a capacity problem rather than a crash loop.

### 5.3 Cognito admin lockout

If the seeded admin's password is lost before anyone else has console
access, reset it without email round-trips:

```bash
aws cognito-idp admin-set-user-password \
  --user-pool-id <UserPoolId, from the stack's resources> \
  --username <AdminEmail> \
  --password '<new-temporary-password>' \
  --permanent
```

`--permanent` skips the "force change password at next sign-in" state,
which matters here because there is no forgotten-password self-service flow
exposed by this console client's OAuth configuration.

---

## 6. Known risks (honesty)

This section names what has **not** been checked against live AWS, following
the same rule the rest of this repo already holds itself to.

**The no-public-MCP-gateway pivot (v0.3.0-rc4 live-launch finding).** An
earlier revision of this stack tried to wire an `AWS::BedrockAgentCore::
GatewayTarget` at the internal MCP ALB (`services.mcp_url`) and hit a real
CloudFormation validation failure, not a hypothetical one: the LIVE registry
schema (`aws cloudformation describe-type --type RESOURCE --type-name
AWS::BedrockAgentCore::GatewayTarget`) requires
`McpServerTargetConfiguration.Endpoint` to match `^https://.*`, and this
stack's only MCP endpoint is `services.py`'s internal ALB — plain HTTP,
`internet_facing=False`, no TLS listener. The repo owner's decision, given
that finding: **no publicly exposed MCP gateway, period.** Standing up an
HTTPS front for an internal ALB just to satisfy a Gateway target's regex was
rejected outright.

**As-built, this stack now looks like:**

1. **No `GatewayTarget` is provisioned.** `agents.py`'s `CfnGateway` and its
   `fde-gateway-m2m` `CfnOAuth2CredentialProvider` both still exist — each
   provisions cleanly with no MCP-endpoint dependency — but the Gateway is
   **provisioned but unwired**: nothing points at `services.mcp_url`
   through it, and no runtime calls it. Wiring a real target is v2 scope,
   gated on an HTTPS-fronted MCP endpoint existing, which in turn is gated
   on the owner's no-public-MCP-gateway decision changing.
2. **All three AgentCore Runtimes join the VPC.** `network_mode="VPC"`
   (the live registry schema's `NetworkMode` enum is `["PUBLIC", "VPC"]` —
   confirmed the same `describe-type` way, not cfn-lint's bundled copy,
   which is exactly what mis-described the GatewayTarget pattern above),
   attached to `network.py`'s private-with-egress subnets through one
   shared security group with a private route opened to Aurora
   (`database.cluster.connections.allow_default_port_from`, the same
   idiom the gate Lambda and migration runner already use). This gives
   `FDE_DB_SECRET_ARN` (tracing writes) a genuine private route it did not
   have before.
3. **Every runtime uses in-container stdio MCP, not the Gateway.**
   `FDE_GATEWAY_URL`/`FDE_GATEWAY_SCOPES` are no longer set on any
   runtime's environment; their absence is exactly what selects
   `mcp_tools.build_mcp_client`'s stdio transport (`fde_agents/common/
   config.py`) — each runtime spawns its own `python -m fde_mcp`
   subprocess and talks to it locally. That subprocess needs its own
   Aurora route, which point 2 supplies.

This resolves the reachability question an earlier revision of this
document left open for surfaces "runtime → Aurora" (both the tracing writes
and the local-MCP-subprocess path) by construction, not by a live test still
pending. The old "Gateway → internal ALB" surface is moot, not resolved —
there is no Gateway target left for that question to apply to. **The v2
precondition**, if the owner ever revisits the no-public-MCP-gateway
decision: stand up an HTTPS-fronted MCP endpoint (e.g. TLS on the internal
ALB plus a reachability story for a non-VPC-attached Gateway, or a different
front entirely), then wire a `CfnGatewayTarget` back into `agents.py` against
it — the schema's `^https://.*` requirement is a hard floor either way.

**Nothing in this stack has run against live AWS.** Every CloudFormation
property name was cross-checked against the installed CDK library, Context7,
and this repo's own boto3-based deploy scripts (three independent sources —
see `agents.py`'s own docstring) — but "verified API shapes" is not the same
claim as "deployed and observed." Budget iteration time for the first real
launch, per the SDD plan's Task 10.

**The `fde-gateway-m2m` OAuth2 credential provider (`CfnOAuth2CredentialProvider`,
`agents.py`) is new in this fix wave and carries the same label.** Its
shape was cross-checked three ways (local CDK introspection, the CDK L2's
own `OAuth2CredentialProviderVendor.COGNITO` enum value, and the installed
`botocore` service model for `bedrock-agentcore-control`) but has not been
created against a live account — if a live test finds the vendor/config
pairing (`CognitoOauth2` + the generic `customOauth2ProviderConfig` shape)
rejected, that is the first place to look.

**AgentCore VPC-mode runtime subnets are pinned by AZ-ID, and that is
us-east-1-only (v0.3.0-rc6 live-launch finding).** `CreateAgentRuntime`
(VPC mode) rejected this stack's original private subnets outright:
"The following subnets are in unsupported availability zones in region
us-east-1: subnet-... in us-east-1b (ID: use1-az6). Supported availability
zones are: use1-az4, use1-az1, use1-az2." AgentCore VPC mode only accepts
that fixed set of AZ-IDs per region, and `network.py`'s VPC selects
subnets by AZ NAME — the AZ-NAME-to-AZ-ID mapping is randomized per AWS
account, so no name-based subnet selection is ever portably correct for
this control plane. The fix (`agents.py`): two dedicated private subnets,
`RuntimeSubnetAz1`/`RuntimeSubnetAz2`, pinned directly to `use1-az1`/
`use1-az2` (as of 2026-08-04) and routed to the stack's single NAT gateway
through the VPC's existing private route table, with every runtime's
`VpcConfig.Subnets` pointed at only those two — not `network.py`'s general
private-with-egress tier. Hardcoding those AZ-IDs makes this template
us-east-1-only, which is already this stack's documented v1 posture (see
`params.py`'s `AssetsRegionMap`, RELEASING.md) — revisit AZ-ID selection
here when this platform ever expands to a second region.

**Costs are estimated, not observed.** Aurora Serverless v2 at the 2-ACU
floor and a single NAT gateway dominate a `demo`-tier month; see docs/09 §9
for the platform's general cost shape (model inference dominates at real
usage, Aurora dominates at idle). Real numbers for this specific stack land
in this document only after Task 10's first live launch — nothing below is
a bill anyone has actually been sent.

---

## 7. docs/10 for a launch-stack deployment

`docs/10-prodops-runbook.md` describes the day-to-day operator experience.
Two of its claims need a launch-stack-specific correction and mapping.

**§4 retraction.** §4 says a `critical`-severity drift signal is
"alert[ed] automatically" to "the compliance channel." **On a launch-stack
deployment, this is not wired up.** The drift-signal-to-SNS-notification
path is a deferred item (tracked in this feature's own build ledger, not
shipped in this stack) — `sor.drift_signal` rows at `critical` severity sit
in the drift queue like any other signal; nothing pages anyone. Until that
wiring exists, a launch-stack operator must poll the drift queue for
`critical` signals rather than trust an alert to arrive.

**§5 escalation mapping.** §5's table names situations and "who to escalate
to" in the abstract. On a launch-stack deployment, here is what each row
actually maps to:

| §5 situation | Actual surface on this stack |
|---|---|
| "A workflow run is stuck or failing and the error doesn't make sense" → Engineering on-call | `fde-gate-service`'s CloudWatch log group (`/aws/lambda/fde-gate-service`); `FdeGateErrors` alarm; `FdeOps` dashboard's gate panel |
| "A proposal touches a compliance control..." → Risk/Compliance reviewer roster | No automated surface — this is an authorization/staffing process, not something the stack pages. The review console itself shows which gates are outstanding |
| "A `control_bypass` drift signal..." → Compliance lead | **No automated surface today** (see the §4 retraction above) — must be found by manually checking the drift queue, not by waiting for a page |
| "...outside the process's normal scope" → Process owner | No automated surface — a human-judgment escalation, same as un-deployed |
| "...drift queue itself is broken" → Engineering on-call | `FdeOpsDlqMessages` alarm (if the drift-scan Lambda, once added — see §8 — is failing to enqueue); the embed-queue and migration-runner panels on the `FdeOps` dashboard as a general "is anything stuck" check |
| "The review queue is growing faster than reviewers can keep up" → reviewer-staffing owner | No automated surface — `FdeOps` dashboard's gate-service invocation panel is the closest observable proxy (rising invocation count with flat approval throughput), not an alarm |

---

## 8. Operationalizing the full platform

This stack is the **core loop** — ingest → propose → review → merge →
search — deliberately, not the full platform. Add the rest as you need it:

**SoR adapters and drift detection.** Not in this stack at all (the design's
own non-goal). Add them via `fde-sor deploy` (the Lambda/EventBridge
serverless path — `packages/fde-sor/src/fde_sor/deploy/sor_lambdas.py`) or
`infra/k8s/` (EKS manifests, if you already run a cluster) once you have a
real system of record to point at. Either path needs its own IAM role and
its own schedule — see docs/08 and docs/09 §7's scheduled-work table.

**Training pipeline.** Not in this stack. Once the review queue has
accumulated enough merged/edited/rejected decisions, run the offline
pipeline in docs/06 — SFT export, then (only past its own volume and
kappa-quality gates) RL. Nothing here provisions SageMaker or any training
compute; that is `packages/fde-training`'s own CLI, run against this
stack's Aurora cluster.

**Hardening checklist** (docs/09 §12 and docs/10 are the general references;
these are the launch-stack-specific items this build knows about and did
not fix, named explicitly rather than left implicit):

- [ ] **Second API-key parameter (the one-vendor-key fix).** §2.1 names the
      limitation: `ProviderApiKey` is the only key parameter this stack has,
      shared between the model and embedding providers, so a cross-vendor
      pairing needing two different keys is not expressible today. v2 fix:
      an `EmbedApiKey` `CfnParameter` + its own Secrets Manager secret,
      mirroring `ProviderApiKey`'s existing shape exactly.
- [ ] **Business-metrics dashboard.** The `FdeOps` dashboard (`ops.py`) is
      infra-only — Lambda/ALB/Fargate/Aurora/queue metrics. Docs/07 §8's
      product-health numbers (median `review_seconds` by gate kind, edit
      rate, expiry rate) have no CloudWatch publisher anywhere in this
      stack; they live only in `hitl.*` tables today and need an
      application-level metrics publisher (or a console-side report) before
      they can appear next to the infra panels.
- [ ] **Per-AgentCore-runtime alarms.** The eight alarms in §4 do not cover
      the three AgentCore Runtimes individually — a runtime invocation
      failure surfaces only indirectly, via `FdeGateErrors` if it also
      breaks a gate-mediated call, not as its own signal. Add per-runtime
      CloudWatch alarms (AgentCore's own emitted metrics — docs/09 §8) once
      a live launch shows which failure modes actually matter to catch.
- [ ] **Per-agent IAM role split.** `runtime_role` (`iam_roles.py`) is one
      shared role for all three AgentCore runtimes, not the least-privilege
      ideal of one role per runtime — the repo's own
      `runtime-trust-policy.json` names this trade explicitly. Splitting it
      means resolving the circularity iam_roles.py documents (a
      per-runtime role's trust condition would need that runtime's ARN,
      which does not exist until the role does).
- [ ] **`gate_role`'s inert Aurora-master-secret read.** Both `gate_role`
      and `runtime_role`'s repo-template permissions grant
      `secretsmanager:GetSecretValue` on the Aurora cluster's *master*
      credentials secret (`ReadTheDbSecret` in each JSON template) — a
      leftover from before the migration-minted per-service login secrets
      (`fde/db/gate`, `fde/db/agent`) existed. Both roles' real DB access
      now goes through a narrower supplemental grant on the login secrets
      instead; the master-secret statement is unused and should be removed
      once confirmed safe to drop.
- [ ] **Bedrock Guardrails.** Not provisioned (design's own non-goal, per
      the spec) — attach one for PII and prompt-injection defense per
      docs/09 §12's checklist before any real customer data flows through.
- [ ] **CloudWatch Transaction Search.** Manual, one-time, account-level —
      not something CDK provisions. See §3, step 8.
- [ ] **Restore drill.** Snapshot-on-delete + 7-day backup retention exist;
      nobody has restored from either yet. Do one before trusting it.
- [ ] **`memory.py`'s literal bugs, left uncorrected on purpose.**
      `packages/fde_agents/deploy/memory.py`'s own strategy names
      (`fde-semantic`, hyphenated) and namespace-template placeholder
      (`{strategyId}`) both violate `AWS::BedrockAgentCore::Memory`'s real
      CloudFormation schema (no hyphens in strategy names; the placeholder
      must be `{memoryStrategyId}`) — caught by `cfn-lint`, the first thing
      in this repo to actually validate those literals. `agents.py` uses
      corrected literals (`fde_semantic`, `{memoryStrategyId}`) rather than
      reproducing the bug, so this stack is unaffected, but `memory.py`
      itself (the boto3 CLI path) still carries the original bug and would
      fail the first time it ran against a live account.
- [ ] **MCP `/healthz` route.** `services.py`'s internal ALB health-checks
      `fde-mcp` on `/mcp` itself with `200-499` treated as healthy, because
      `fde_mcp/server.py` has no dedicated health-check route today. A real
      `@mcp.custom_route("/healthz", ...)` in that package would be a
      strictly better signal; out of scope for this CDK-only build.
- [ ] **`TreatMissingData` on the count-based alarms.** None of the eight
      alarms in §4 set it explicitly (CloudWatch's `missing` default).
      Consider `notBreaching` for the count-style alarms (DLQ messages,
      unhealthy hosts) so a metric that simply has no data yet (e.g.
      immediately after launch) does not read as ambiguous.
- [ ] **Teardown sweep** — see §9; three categories of resource this stack
      creates but CloudFormation does not track, and so does not clean up
      on `delete-stack`.

---

## 9. Teardown

`aws cloudformation delete-stack --stack-name FdePlatform` (or the console
equivalent) removes everything CloudFormation created. Two things are kept
on purpose:

- **A final Aurora snapshot** — `RemovalPolicy.SNAPSHOT` on the cluster.
- **The Cognito user pool** — `RemovalPolicy.RETAIN`. Your accounts survive
  a stack deletion; delete the pool by hand if you actually want it gone.

**Manual sweep — resources this stack creates outside of CloudFormation's
tracking, so `delete-stack` will not touch them:**

1. **`fde/db/agent`, `fde/db/gate`, `fde/db/ingest` Secrets Manager
   secrets.** Minted by the migration Lambda's own `boto3.create_secret`
   calls (custom-resource side effects, not `AWS::SecretsManager::Secret`
   resources CloudFormation owns) — they are orphaned on every stack
   deletion. Delete them by hand (`aws secretsmanager delete-secret
   --secret-id fde/db/agent`, etc.) if you are done with this launch for
   good; leave them if you plan to relaunch against the same login roles.
2. **Any pre-7.5 orphaned service-created log groups.** If you relaunched a
   build predating Task 7.5's explicit `LogGroup` resources, AWS Lambda's
   own auto-created `/aws/lambda/fde-gate-service` group (2-year default
   retention) collides on create with this stack's CloudFormation-owned one
   of the identical name. Delete the old one by hand before a fresh launch
   if you hit `ResourceAlreadyExistsException` at deploy time.
3. **ECR pull-through-cache repositories.** The `AWS::ECR::PullThroughCacheRule`
   resource itself is deleted with the stack, but the actual cached
   repositories it creates lazily on first pull
   (`{account}.dkr.ecr.{region}.amazonaws.com/ecr-public/{alias}/fde-*`) are
   not CloudFormation-tracked resources and are not deleted with it. Sweep
   them with `aws ecr batch-delete-image`/`delete-repository` if you want a
   fully clean account.
