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
uv run cfn-lint cdk.out/FdePlatform.template.json -i W2001 W7001 W8001   # validate the emitted template
uv run ruff check .                                           # lint
uv run ruff format --check .                                  # format check
uv lock --check                                                # lockfile hasn't drifted from pyproject.toml
```

One-liner (synth + lint), what CI runs:

```bash
uv run pytest tests && uv run python app.py && uv run cfn-lint cdk.out/FdePlatform.template.json -i W2001 W7001 W8001
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
| `fde_cdk/stack.py` | `FdePlatformStack` — the root stack. Bootstrap-free synthesizer lives here; wires the `Network`, `Database`, `Identity`, and `IamRoles` constructs (and later tasks' constructs) into itself, not new stacks, in that fixed order |
| `fde_cdk/network.py` | `Network` — the one VPC (2 AZ, 1 NAT, public + private-with-egress) everything else attaches to |
| `fde_cdk/database.py` | `Database` — the Aurora PostgreSQL (pgvector) cluster, tier-switched between Serverless v2 (demo) and provisioned `db.r6g.xlarge` (production) via `Fn::If` on `is_production` |
| `fde_cdk/identity.py` | `Identity` — one Cognito user pool: admin-seeded human login (`console_client`, hosted UI, auth-code grant), a stable JWT audience (`api_client`), and a client-credentials M2M client scoped to `gateway/invoke` (`m2m_client`) |
| `fde_cdk/iam_roles.py` | `IamRoles` — every execution role, rendered from the repo's own checked-in IAM policy JSON (`packages/fde-agents/.../deploy/iam/*.json`, `packages/fde-gate/.../deploy/iam/*.json`) via `_role_from_template`'s `${VAR}` substitution, plus three roles derived from `gateway.py`/`memory.py`'s API calls and the migration Lambda's stated needs (no repo template for those three) |
| `tests/test_synth.py` | `synth_template()` helper (imported by later tasks' tests) plus the two contract tests: synthesizes, and never touches CDK-bootstrap assets |
| `tests/test_network_db.py` | VPC topology, Aurora engine/snapshot-policy, and secret-shape tests for `Network`/`Database` |
| `tests/test_identity_iam.py` | Cognito pool/client/domain/resource-server shape, IAM role trust/permissions substitution, and the fixed `fde-gate-service` function-name tests for `Identity`/`IamRoles` |

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

## Unused-parameter/condition/mapping warnings — still expected, narrower

Task 2 created the full `Parameters`/`Conditions`/`Mappings` click surface
before any construct existed to consume it. `cfn-lint` correctly flags this
as suspicious: `W2001` (parameter never referenced), `W8001` (condition
never referenced), `W7001` (mapping never referenced). These are genuine
warnings about a template that is, right now, deliberately incomplete — not
a false positive to silence structurally the way `E1001` was. `cfn-lint` is
invoked with `-i W2001 W7001 W8001` to ignore exactly those three rule IDs
and nothing else; every other rule (including all `E` rules) still gates the
build.

Task 3 wires `deploy_tier`/`is_production` into the database construct
(`DBInstanceClass` and `DeletionProtection`, both `Fn::If`'d on
`IsProduction`), which is the first entity in any of these three rules to
gain a real consumer. Verified with a bare `cfn-lint
cdk.out/FdePlatform.template.json` (no `-i`) before and after this task:

| Rule | Before Task 3 | After Task 3 | After Task 4 |
|---|---|---|---|
| `W2001` (param unused) | 7 (`ModelId`, `CompatBaseUrl`, `EmbedProvider`, `EmbedModelId`, `AdminEmail`, `ReleaseTag`, `AssetsBucket`) | 7 — unchanged | 6 (`AdminEmail` dropped — `Identity` reads `params.admin_email.value_as_string` to seed the Cognito admin user) |
| `W8001` (condition unused) | 3 (`IsProduction`, `IsBedrockModel`, `HasProviderKey`) | 2 (`IsProduction` dropped) | 2 — unchanged |
| `W7001` (mapping unused) | 1 (`AssetsRegionMap`) | 1 — unchanged | 1 — unchanged |

`DeployTier`, `ModelProvider`, and `ProviderApiKey` were never in the `W2001`
list even in Task 2: cfn-lint's `Ref` scan already counted them "used" via
their own (otherwise-unconsumed) `CfnCondition` expressions
(`IsProduction`/`IsBedrockModel`/`HasProviderKey`), which is a materially
different check from `W8001`'s "is this *condition* used by a resource or
`Fn::If`" — so wiring `is_production` into a resource could only ever move
the `W8001` count, not `W2001`'s. `AssetsRegionMap` (`W7001`) and the other
two conditions (`W8001`) stay unconsumed until later tasks (the migration
Lambda's code location, the model-provider/API-key secrets) reach them.

Since every one of the three rule IDs still fires at least once, **the
`-i` list does not shrink after Task 3 or Task 4** — removing any of the
three would break `cdk-lint` in CI. Task 4's `Identity`/`IamRoles`
constructs consume `AdminEmail` (moving the `W2001` count from 7 to 6, see
table above) but touch none of the `W8001` conditions or the `W7001`
mapping — those are read by later tasks (the migration Lambda's code
location for `AssetsRegionMap`/`W7001`; the model-provider/API-key secrets
for `IsBedrockModel`/`HasProviderKey`/`W8001`). Revisit the bare-lint check
after each of tasks 5–7 lands its constructs; delete a rule ID from the
`-i` list (both here and in `.github/workflows/ci.yml`) only once a bare
run shows zero remaining occurrences of it.
