# infra/cdk

CDK project behind the FDE Agent Platform's README Launch-Stack button. This
is a standalone `uv` project (its own `pyproject.toml` / `uv.lock` / `.venv`)
— it is **not** a workspace member of the repo root and never installs into
that venv. Every command here runs from this directory:

```bash
cd infra/cdk && uv run <command>
```

## Why no Node.js / `aws-cdk` CLI

`cdk.json`'s `app` command is `uv run python app.py` — synthesis happens by
calling `app.synth()` directly inside `app.py`, not via the Node `cdk` CLI.
There is nothing to `npm install`. Template validation is done by `cfn-lint`
against the emitted JSON, not `cdk synth`'s own diff/deploy tooling.

## The bootstrap-free contract

The whole point of this stack is a **one-click launch**: someone with a
brand-new AWS account, who has never run `cdk bootstrap`, clicks a
CloudFormation "Launch Stack" link and it just works. That means the
synthesized template must never reference the CDK bootstrap stack's assets
bucket (`cdk-hnb659fds-assets...`) or its `BootstrapVersion` SSM parameter —
both of which appear the moment any construct in the tree uses a CDK asset
(inline Lambda code from a local path, a `DockerImageAsset`, the default
`BucketDeployment`, etc.).

`FdePlatformStack` enforces this **mechanically**, not by convention: it is
constructed with `synthesizer=cdk.BootstraplessSynthesizer()`. That
synthesizer has no asset bucket to publish to, so any construct that tries to
use one raises at synth time — the violation is caught before it ever reaches
a template file. `tests/test_synth.py::test_no_cdk_assets_anywhere` is the
second, independent check: it greps the synthesized JSON for the bootstrap
qualifier and the `BootstrapVersion` parameter directly. Every later task's
constructs must synth clean through both of these; if either one fails,
that's a design violation in the construct, not something to relax in the
test.

## Commands

```bash
uv sync --frozen                                             # install deps exactly as locked
uv run pytest tests -v                                       # synth tests
uv run python app.py                                         # synth -> cdk.out/FdePlatform.template.json
uv run cfn-lint cdk.out/FdePlatform.template.json -i W3005   # validate the emitted template
uv run ruff check .                                           # lint
uv run ruff format --check .                                  # format check
uv lock --check                                                # lockfile hasn't drifted from pyproject.toml
```

One-liner (synth + lint), what CI runs:

```bash
uv run pytest tests && uv run python app.py && uv run cfn-lint cdk.out/FdePlatform.template.json -i W3005
```

Note the `-i` flags are positioned AFTER the template path: `cfn-lint`'s
`-i`/`--ignore-checks` takes an unbounded list of rule IDs (`nargs='+'`), so
if it comes first on the command line it greedily swallows the template
path too, leaving cfn-lint with no `TEMPLATE` argument at all (it then
falls back to validating an effectively-empty template and reports
`E1001` -- a confusing false error). `cfn-lint TEMPLATE -i CODE...` is the
only safe order.

## aws-cdk-lib version and `aws_bedrockagentcore` availability

Later tasks provision AgentCore resources (runtimes, gateways, memory, ...)
via CDK's L1 (`Cfn*`) constructs in `aws_cdk.aws_bedrockagentcore`. That
module does not exist in every `aws-cdk-lib` release; this project pins one
where it does.

- **Working version:** `aws-cdk-lib==2.263.0` (installed via
  `aws-cdk-lib>=2.220`, resolved by `uv add` at implementation time).
- **Verification command:**

  ```bash
  uv run python -c "import aws_cdk.aws_bedrockagentcore as bac; print([n for n in dir(bac) if n.startswith('Cfn')])"
  ```

- **Output** (the `Cfn*` types this version provides):

  ```
  ['CfnApiKeyCredentialProvider', 'CfnApiKeyCredentialProviderProps', 'CfnBrowser',
  'CfnBrowserCustom', 'CfnBrowserCustomProps', 'CfnBrowserProfile', 'CfnBrowserProfileProps',
  'CfnBrowserProps', 'CfnCodeInterpreterCustom', 'CfnCodeInterpreterCustomProps',
  'CfnConfigurationBundle', 'CfnConfigurationBundleProps', 'CfnDataset', 'CfnDatasetProps',
  'CfnEvaluator', 'CfnEvaluatorProps', 'CfnGateway', 'CfnGatewayProps', 'CfnGatewayTarget',
  'CfnGatewayTargetProps', 'CfnHarness', 'CfnHarnessProps', 'CfnMemory', 'CfnMemoryProps',
  'CfnOAuth2CredentialProvider', 'CfnOAuth2CredentialProviderProps', 'CfnOnlineEvaluationConfig',
  'CfnOnlineEvaluationConfigProps', 'CfnPaymentConnector', 'CfnPaymentConnectorProps',
  'CfnPaymentCredentialProvider', 'CfnPaymentCredentialProviderProps', 'CfnPaymentManager',
  'CfnPaymentManagerProps', 'CfnPolicy', 'CfnPolicyEngine', 'CfnPolicyEngineProps',
  'CfnPolicyProps', 'CfnResourcePolicy', 'CfnResourcePolicyProps', 'CfnRuntime',
  'CfnRuntimeEndpoint', 'CfnRuntimeEndpointProps', 'CfnRuntimeProps', 'CfnWorkloadIdentity',
  'CfnWorkloadIdentityProps']
  ```

  Relevant for this platform: `CfnRuntime` / `CfnRuntimeEndpoint` (the three
  AgentCore agent runtimes), `CfnGateway` / `CfnGatewayTarget` (MCP tool
  surface), `CfnMemory`, `CfnWorkloadIdentity`. No `CfnAgent` type exists in
  this version — runtimes are the deployable unit, matching how
  `fde_agents/deploy` already models them.

## Layout

| Path | Role |
|---|---|
| `app.py` | CDK app entry point (`uv run python app.py`); builds `FdePlatformStack` and calls `app.synth()` with an explicit `outdir="cdk.out"` |
| `cdk.json` | Tells any CDK tooling how to run the app: `{"app": "uv run python app.py"}` |
| `fde_cdk/stack.py` | `FdePlatformStack` — the root stack. Bootstrap-free synthesizer lives here; wires `Network` -> `Database` -> `Identity` -> `IamRoles` -> `Migrations` -> (the shared `provider_api_key_secret`) -> `Services` -> `GateService` into itself, not new stacks. `Services` before `GateService`: the gate Lambda's `FDE_MCP_URL` env needs `services.mcp_url`. Task 7's `Agents` construct will insert between `Services` and `GateService` (see `gate.py`'s module docstring) |
| `fde_cdk/network.py` | `Network` — the one VPC (2 AZ, 1 NAT, public + private-with-egress) everything else attaches to |
| `fde_cdk/database.py` | `Database` — the Aurora PostgreSQL (pgvector) cluster, tier-switched between Serverless v2 (demo) and provisioned `db.r6g.xlarge` (production) via `Fn::If` on `is_production` |
| `fde_cdk/identity.py` | `Identity` — one Cognito user pool: admin-seeded human login (`console_client`, hosted UI, auth-code grant), a stable JWT audience (`api_client`), and a client-credentials M2M client scoped to `gateway/invoke` (`m2m_client`) |
| `fde_cdk/iam_roles.py` | `IamRoles` — every execution role, rendered from the repo's own checked-in IAM policy JSON (`packages/fde-agents/.../deploy/iam/*.json`, `packages/fde-gate/.../deploy/iam/*.json`) via `_role_from_template`'s `${VAR}` substitution, plus three roles derived from `gateway.py`/`memory.py`'s API calls and the migration Lambda's stated needs (no repo template for those three) |
| `fde_cdk/migrations.py` | `Migrations` — the VPC-attached migration-runner Lambda (code from S3 via the `HasAssetsBucket` `Fn::If`, never a CDK asset) plus the Provider-free `AWS::CloudFormation::CustomResource` that invokes it once per deploy |
| `lambdas/migration_runner/handler.py` | The Lambda's own code: `plan_migrations` (pure, forward-only planner), the psycopg/boto3 migration+role+secret+cfn-response logic (both imported lazily so `plan_migrations` stays importable without either dependency installed) |
| `lambdas/migration_runner/build.py` | Builds `dist/migration-runner.zip`: `handler.py` + a copy of repo-root `db/0*.sql` + `psycopg[binary]` wheels for `aarch64-manylinux_2_28` (same pattern as `fde_gate/deploy/package.py`) |
| `tests/test_synth.py` | `synth_template()` helper (imported by later tasks' tests) plus the two contract tests: synthesizes, and never touches CDK-bootstrap assets |
| `tests/test_network_db.py` | VPC topology, Aurora engine/snapshot-policy, and secret-shape tests for `Network`/`Database` |
| `tests/test_identity_iam.py` | Cognito pool/client/domain/resource-server shape, IAM role trust/permissions substitution, and the fixed `fde-gate-service` function-name tests for `Identity`/`IamRoles` |
| `tests/test_migration_runner.py` | `plan_migrations` unit tests (imports `handler.py` directly, no AWS) plus synth tests for the custom resource, the Lambda's code location/VPC attachment, and the Provider-free contract |
| `fde_cdk/gate.py` | `GateService` — the `fde-gate-service` Lambda (VPC-attached, code from S3, `function_name=GATE_FUNCTION_NAME` verbatim), its `apigatewayv2.HttpApi` (JWT-authorized `ANY /{proxy+}`, unauthenticated `GET /healthz`), and the two EventBridge schedules. Also `provider_api_key_secret`/consumed via `params.dynamic_secret_env_value` — the one Secrets Manager mirror of `ProviderApiKey` shared with `Services` |
| `fde_cdk/services.py` | `Services` — one `ecs.Cluster`; `mcp_service` (ARM64 Fargate, `{ECR_PUBLIC_BASE}/fde-mcp:{tag}`) behind an internal ALB (`mcp_url` output); `embedder_service` (same image, `command=["fde-embedder"]`, no ALB) |
| `tests/test_services.py` | Synth tests for `GateService` (Lambda shape, HTTP API routes/authorizer, schedules, the supplemental `gate_role` secret grant) and `Services` (internal ALB, ARM64 task definitions, container image/env/command, the shared provider-API-key secret's conditional wiring, the Migrations dependency) |

## A quirk resolved in Task 3: `analytics_reporting`

Through Task 2, `app.py` passed `analytics_reporting=True` to `cdk.App(...)`
as a workaround: the Node `cdk` CLI turns this on by default, which is where
CDK's standard (harmless, asset-free) `AWS::CDK::Metadata` resource normally
comes from, and this project bypasses that CLI entirely (so it defaults to
off). A template with literally zero resources fails CloudFormation's own
schema (`cfn-lint` E1001, "'Resources' is a required property"), and Task 2's
`Parameters`/`Conditions`/`Mappings`-only template had exactly that problem —
those are separate template sections from `Resources`.

Task 3's `Network`/`Database` constructs give the stack its first genuine
resources, so the workaround is no longer needed: `app.py` no longer passes
`analytics_reporting` at all (its default is already `False`). The forcing
functions that tracked this — `tests/test_params.py::test_no_cdk_metadata_resource`
(was `xfail(strict=False)`, now a plain passing assertion) and
`test_cdk_metadata_present_via_real_app_config` (inverted into
`test_no_cdk_metadata_via_real_app_config`, which now proves the real
`app.py` invocation stays clean *without* the flag) — were both resolved in
the same commit that added `network.py`/`database.py`.

## cfn-lint `-i` ignore list — retired after Task 6, `W3005` only now

Task 2 created the full `Parameters`/`Conditions`/`Mappings` click surface
before any construct existed to consume it. `cfn-lint` correctly flagged this
as suspicious: `W2001` (parameter never referenced), `W8001` (condition
never referenced), `W7001` (mapping never referenced). These were genuine
warnings about a template that was, at the time, deliberately incomplete —
not a false positive to silence structurally the way `E1001` was. `cfn-lint`
was invoked with `-i W2001 W7001 W8001` (later just `-i W2001 W8001`, after
Task 5 retired `W7001`) to ignore exactly those rule IDs and nothing else;
every other rule (including all `E` rules) still gated the build throughout.

Task 3 wires `deploy_tier`/`is_production` into the database construct
(`DBInstanceClass` and `DeletionProtection`, both `Fn::If`'d on
`IsProduction`), which is the first entity in any of these three rules to
gain a real consumer. Verified with a bare `cfn-lint
cdk.out/FdePlatform.template.json` (no `-i`) before and after this task:

| Rule | Before Task 3 | After Task 3 | After Task 4 | After Task 5 | After Task 6 |
|---|---|---|---|---|---|
| `W2001` (param unused) | 7 (`ModelId`, `CompatBaseUrl`, `EmbedProvider`, `EmbedModelId`, `AdminEmail`, `ReleaseTag`, `AssetsBucket`) | 7 — unchanged | 6 (`AdminEmail` dropped — `Identity` reads `params.admin_email.value_as_string` to seed the Cognito admin user) | 4 (`ReleaseTag`, `AssetsBucket` dropped — both are now `Migrations` properties/code-location inputs) | **0 — dropped from the `-i` list.** `ModelId` (`gate.py`'s `FDE_MODEL_ID` env, `Fn::If`'d on `IsBedrockModel`), `CompatBaseUrl` (`FDE_MODEL_BASE_URL`/`FDE_EMBED_BASE_URL`), `EmbedProvider`/`EmbedModelId` (`services.py`'s embed-provider envs) all gain their first consumer here |
| `W8001` (condition unused) | 3 (`IsProduction`, `IsBedrockModel`, `HasProviderKey`) | 2 (`IsProduction` dropped) | 2 — unchanged | 2 — unchanged (the new `HasAssetsBucket` condition is used immediately, so it never appears here) | **0 — dropped from the `-i` list.** `IsBedrockModel` (the `FDE_MODEL_ID` `Fn::If` above) and `HasProviderKey` (the provider-API-key secret's own `Condition:` plus the `Fn::If` wrapping every `{{resolve:secretsmanager:...}}` env value) both gain consumers |
| `W7001` (mapping unused) | 1 (`AssetsRegionMap`) | 1 — unchanged | 1 — unchanged | **0 — dropped from the `-i` list.** `Migrations`' `Fn::If(HasAssetsBucket, ..., FindInMap(AssetsRegionMap, ...))` is `AssetsRegionMap`'s first consumer | 0 — unchanged |
| `W3005` (redundant `DependsOn`) | n/a | n/a | n/a | 1 (new rule ID, added to `-i`) — see below | 3 (predicted: `GateService`'s Lambda reproduces the same explicit-`role=`-plus-VPC pattern as `Migrations`'; unpredicted third: `Services`' `ecs.FargateService` gets one too, between the ALB target group and the service's own `LoadBalancers` property) |

`DeployTier`, `ModelProvider`, and `ProviderApiKey` were never in the `W2001`
list even in Task 2: cfn-lint's `Ref` scan already counted them "used" via
their own (otherwise-unconsumed) `CfnCondition` expressions
(`IsProduction`/`IsBedrockModel`/`HasProviderKey`), which is a materially
different check from `W8001`'s "is this *condition* used by a resource or
`Fn::If`" — so wiring `is_production` into a resource could only ever move
the `W8001` count, not `W2001`'s.

Task 5's `Migrations` construct is the first (and, per the launch-stack
plan, only) consumer of `ReleaseTag`, `AssetsBucket`, and `AssetsRegionMap`
— all three were deliberately unconsumed placeholders since Task 2. That
retires `W7001` entirely (zero remaining `Fn::FindInMap`-eligible mappings
once `AssetsRegionMap` has a real caller) and shrinks `W2001` by two.
`IsBedrockModel`/`HasProviderKey` (`W8001`) are untouched by this task —
they wait for Task 6/7's model-provider/API-key secrets — so `W8001` stays
in the `-i` list at its Task 4 count.

`W3005` is new since Task 5 and, unlike the other three, is **not** expected
to shrink to zero as later tasks land: CDK's `lambda_.Function` L2
automatically adds an explicit `DependsOn` on an externally-passed `role=`
(IAM eventual consistency: the role and its inline policy must finish
propagating before the Lambda's first invoke, not just before the
CloudFormation resource is marked CREATE_COMPLETE) even though the
function's `Role` property already carries an implicit `Fn::GetAtt`
dependency on the same resource — cfn-lint's static DAG check flags the
explicit one as redundant, but removing it would remove real
eventual-consistency protection CDK added on purpose. Task 6's `GateService`
Lambda reproduced this exact warning, as predicted; `Services`' ECS pattern
(`FargateService` + `ApplicationListener.add_targets`) reproduces the same
*class* of warning for a different reason — CDK adds an explicit
`DependsOn` between the service and its target group alongside the
`LoadBalancers[].TargetGroupArn` `Ref` that already implies it. Both are
expected, not regressions to chase down.

**Task 6 retired `W2001` and `W8001` from the `-i` list entirely** (a bare
`cfn-lint cdk.out/FdePlatform.template.json`, no `-i` at all, now reports
only three `W3005` occurrences — verified directly, see the table above) --
`gate.py`'s `FDE_MODEL_ID`/`FDE_MODEL_BASE_URL`/`FDE_MODEL_API_KEY` envs and
`services.py`'s embed-provider envs gave every remaining unconsumed
parameter and condition its first consumer. The `-i` list (here and in
`.github/workflows/ci.yml`) is now just `-i W3005`, expected to stay that
way permanently: any later task that gives another Lambda/Fargate construct
both an explicit `role=`/target-group wiring and its own VPC attachment
will very likely reproduce it again, and that is fine.
