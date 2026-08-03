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
uv run cfn-lint cdk.out/FdePlatform.template.json             # validate the emitted template
uv run ruff check .                                           # lint
uv run ruff format --check .                                  # format check
```

One-liner (synth + lint), what CI runs:

```bash
uv run pytest tests && uv run python app.py && uv run cfn-lint cdk.out/FdePlatform.template.json
```

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
E1001, "'Resources' is a required property"). This won't matter in practice
once task 2 adds real constructs, but was needed to make `cfn-lint` clean
for this scaffold task, which has no resources yet by design.
