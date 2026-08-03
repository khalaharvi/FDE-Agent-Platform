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
| `fde_cdk/stack.py` | `FdePlatformStack` — the root stack. Bootstrap-free synthesizer lives here; later tasks add constructs to this stack, not new stacks |
| `tests/test_synth.py` | `synth_template()` helper (imported by later tasks' tests) plus the two contract tests: synthesizes, and never touches CDK-bootstrap assets |

## A currently-expected quirk: `analytics_reporting=True`

`app.py` passes `analytics_reporting=True` to `cdk.App(...)`. The Node `cdk`
CLI turns this on by default, which is where CDK's standard (harmless,
asset-free) `AWS::CDK::Metadata` resource normally comes from. Since this
project bypasses that CLI entirely, it defaults to off — and a template with
literally zero resources fails CloudFormation's own schema (`cfn-lint`
E1001, "'Resources' is a required property").

Task 2 (parameters, conditions, the region mapping) does **not** remove this
need: `Parameters`/`Conditions`/`Mappings` are separate template sections
from `Resources`, so a parameters-only template still has an empty
`Resources` section without this flag. The flag stays `True` until a later
task's first real construct (network/database, task 3) gives the stack a
genuine resource — `tests/test_params.py::test_no_cdk_metadata_resource` is
an `xfail(strict=False)` forcing-function test that should be un-xfailed (and
this flag flipped to `False`) in that same commit.
`test_cdk_metadata_present_via_real_app_config` in the same file documents
today's actual state precisely, since `tests/test_synth.py`'s
`synth_template()` helper builds its own bare `cdk.App()` and therefore never
exercises this flag at all — only the real `app.py` invocation does.

## Another currently-expected quirk: unused-parameter/condition/mapping warnings

Task 2 creates the full `Parameters`/`Conditions`/`Mappings` click surface
before any construct exists to consume it (that's tasks 3–7). `cfn-lint`
correctly flags this as suspicious: `W2001` (parameter never referenced),
`W8001` (condition never referenced), `W7001` (mapping never referenced).
These are genuine warnings about a template that is, right now, deliberately
incomplete — not a false positive to silence structurally the way `E1001`
was. `cfn-lint` is invoked with `-i W2001 W7001 W8001` to ignore exactly
those three rule IDs and nothing else; every other rule (including all `E`
rules) still gates the build. As tasks 3–7 wire each parameter/condition/
mapping into a real resource (`is_production` into the database construct's
removal policy, `AssetsRegionMap` into the migration Lambda's code location,
etc.), the warning for that specific entity disappears on its own — the
`-i` list does not need to shrink, but it's worth deleting once every
parameter, condition, and the mapping all have a real consumer (verify with
a bare `cfn-lint cdk.out/FdePlatform.template.json`, no `-i`, going clean).
