# One-Click AWS Deploy (Launch Stack) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A README Launch-Stack button whose hosted CloudFormation template deploys the core loop (VPC → Aurora/pgvector → migrations → Cognito → gate Lambda + review console → MCP on Fargate/ALB → embedder → 3 AgentCore runtimes + Gateway + Memory) with provider choice at launch — live-validated before the button ships.

**Architecture:** A CDK-Python app in isolated `infra/cdk/` (own venv, NOT a workspace member) synthesizes one `FdePlatformStack` from eight single-responsibility constructs. The synthesized template is bootstrap-free: no CDK assets — Lambda zips and the migration bundle resolve from a release assets bucket via parameters, container images from ECR Public (agents via an in-account pull-through cache). A tag-triggered `release.yml` builds/pushes all artifacts and the template. TDD vehicle for all CDK work: `aws_cdk.assertions.Template` unit tests + cfn-lint, both offline.

**Tech Stack:** aws-cdk-lib (with `aws_bedrockagentcore` L2/L1), constructs, pytest, cfn-lint, GitHub Actions, ECR Public, psycopg (migration runner).

**Spec:** `docs/superpowers/specs/2026-08-02-one-click-aws-deploy-design.md` (committed on this branch). Deploy-surface facts referenced throughout come from the repo's own deploy modules — implementers read those files, they are the source of truth for API payload shapes.

## Global Constraints

- Branch: `feat/one-click-deploy` (stacked on `feat/multi-provider-llm`; if PR #1 has merged, FIRST rebase onto `origin/main` before new work). PR at the end; **the user merges — never merge, never enable auto-merge.**
- **Bootstrap-free template:** the synthesized template must deploy in a fresh AWS account with NO CDK bootstrap. Therefore: zero CDK assets (`s3_assets`, `DockerImageAsset`, `lambda.Code.from_asset` are all FORBIDDEN). Lambda code = `lambda_.Code.from_bucket(<assets bucket resolved from parameter/mapping>, key)`. Images = ECR Public URIs (Fargate directly; AgentCore via pull-through cache into private ECR).
- **v1 region: us-east-1 only** (assets bucket must be same-region as Lambda code). Region list extension is documented, not built.
- `infra/cdk/` is an ISOLATED uv project: own `pyproject.toml` + `uv.lock` + `.venv`; never `uv add` these deps into the workspace (the `train`-extra venv incident is the precedent). All commands run as `cd infra/cdk && uv run ...`.
- Workspace packages are untouched except: `.github/workflows/` (new release.yml, new cdk-lint CI job, deploy-job fix), `README.md` (button placeholder, held), `docs/13-launch-stack.md`, `CLAUDE.md` (one line). NO changes to `packages/*` or `db/*` — the migration runner bundles `db/*.sql` at build time, it does not modify them.
- Engine/version floors (from the spec/docs/09, verify exact minors at implementation): Aurora PostgreSQL at the pgvector ≥ 0.8.0 floor (docs/09 names Aurora PG 16.8+); `db/001` refuses below pgvector 0.8.
- Existing IAM policy JSON templates (`packages/*/src/*/deploy/iam/*.json`) are consumed by rendering their `${...}` placeholders in CDK — do not edit the JSON files themselves.
- The README button is **held** (HTML comment) until the Task 10 live-test gate passes. Honesty labeling: everything ships "written against verified API shapes, not validated" until then.
- Commit messages end with `Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>`. Stage explicit paths only.
- Python conventions apply inside `infra/cdk` too: `from __future__ import annotations`, ruff-clean (add infra/cdk to its own ruff config, NOT the workspace's mypy — CDK's typing is its own world; `uv run ruff check .` inside infra/cdk).
- AgentCore CDK API: `aws_cdk.aws_bedrockagentcore` L2s are stable per AWS docs. If an L2 lacks a needed property, drop to the L1 `Cfn*` type — property shapes mirror the control-plane API already used by `packages/fde-agents/src/fde_agents/deploy/{runtimes,gateway,memory}.py` (READ those files for exact field names). Never invent properties: check with context7 (`/aws/aws-cdk` docs) or the CDK Python API reference before writing each AgentCore construct, and pin what you used in the code comments.

---

### Task 1: CDK project scaffold + synth test harness + CI lint job

**Files:**
- Create: `infra/cdk/pyproject.toml`, `infra/cdk/cdk.json`, `infra/cdk/app.py`, `infra/cdk/fde_cdk/__init__.py`, `infra/cdk/fde_cdk/stack.py`, `infra/cdk/tests/test_synth.py`, `infra/cdk/README.md`, `infra/cdk/.gitignore` (`.venv/`, `cdk.out/`, `uv.lock` is COMMITTED)
- Modify: `.github/workflows/ci.yml` (new `cdk-lint` job)

**Interfaces:**
- Produces: `FdePlatformStack(app, "FdePlatform", ...)` in `fde_cdk/stack.py`; test helper `synth_template() -> aws_cdk.assertions.Template` in `tests/test_synth.py` that later tasks' tests import (`from tests.test_synth import synth_template` — keep it a plain function in that module); the `cdk.json` app command `uv run python app.py`.

- [ ] **Step 1: Scaffold.**

```bash
mkdir -p infra/cdk/fde_cdk infra/cdk/tests
cd infra/cdk && uv init --no-workspace --name fde-cdk --python 3.12
uv add "aws-cdk-lib>=2.220" "constructs>=10.0"
uv add --dev pytest cfn-lint ruff
python -c "import aws_cdk.aws_bedrockagentcore as bac; print([n for n in dir(bac) if n.startswith('Cfn')])"
```
The last command verifies the AgentCore module exists and prints the available `Cfn*` types — paste its output into `infra/cdk/README.md` (it documents what this lib version provides). If the import fails, bump `aws-cdk-lib` until it succeeds; record the working version.

`infra/cdk/pyproject.toml` must set `[tool.uv] package = false` (app, not a library). `cdk.json`: `{"app": "uv run python app.py"}`.

- [ ] **Step 2: Failing synth test** in `infra/cdk/tests/test_synth.py`:

```python
from __future__ import annotations

import json
from pathlib import Path

import aws_cdk as cdk
from aws_cdk.assertions import Template


def synth_template() -> Template:
    app = cdk.App()
    from fde_cdk.stack import FdePlatformStack  # local import: keep collection cheap

    stack = FdePlatformStack(app, "FdePlatform")
    return Template.from_stack(stack)


def test_stack_synthesizes() -> None:
    template = synth_template()
    assert template.to_json()["Description"].startswith("FDE Agent Platform")


def test_no_cdk_assets_anywhere() -> None:
    """The launch-button contract: a fresh account with no CDK bootstrap.
    Any cdk-managed asset parameter or bootstrap-version rule breaks it."""
    raw = json.dumps(synth_template().to_json())
    assert "cdk-hnb659fds-assets" not in raw  # default bootstrap qualifier
    assert "BootstrapVersion" not in synth_template().to_json().get("Parameters", {})
```

- [ ] **Step 3: Run → FAIL** (`cd infra/cdk && uv run pytest tests/ -v` — ModuleNotFoundError for fde_cdk.stack).
- [ ] **Step 4: Minimal stack.** `fde_cdk/stack.py`:

```python
from __future__ import annotations

import aws_cdk as cdk
from constructs import Construct


class FdePlatformStack(cdk.Stack):
    """Root stack behind the README Launch-Stack button. One template URL,
    eight single-responsibility constructs (added by later tasks)."""

    def __init__(self, scope: Construct, construct_id: str, **kwargs: object) -> None:
        super().__init__(
            scope,
            construct_id,
            description=(
                "FDE Agent Platform - core loop launch stack "
                "(knowledge graph, human gates, agents)"
            ),
            synthesizer=cdk.BootstraplessSynthesizer(),
            **kwargs,  # type: ignore[arg-type]
        )
```
`BootstraplessSynthesizer` is what enforces the no-assets contract at synth time — any accidental asset use raises. `app.py` builds the app + stack and calls `app.synth()`.

- [ ] **Step 5: Tests PASS; cfn-lint clean.** `cd infra/cdk && uv run pytest tests/ -v && uv run python app.py && uv run cfn-lint cdk.out/FdePlatform.template.json` (add a `synth-and-lint` one-liner to infra/cdk/README.md).
- [ ] **Step 6: CI job.** In `.github/workflows/ci.yml`, add after `static`:

```yaml
  cdk-lint:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: astral-sh/setup-uv@v5
      - name: synth and lint the launch stack (no AWS credentials)
        working-directory: infra/cdk
        run: |
          uv sync --frozen
          uv run pytest tests
          uv run python app.py
          uv run cfn-lint cdk.out/FdePlatform.template.json
```
Do NOT add this job to the branch-protection required list yet — that happens when the PR merges (note it in the PR body for the user).
- [ ] **Step 7: Commit** (`infra/cdk/**`, `.github/workflows/ci.yml`): `feat: CDK launch-stack scaffold -- bootstrap-free synth + CI lint`

---

### Task 2: Parameters, Mappings, Conditions (the click surface)

**Files:**
- Create: `infra/cdk/fde_cdk/params.py`, `infra/cdk/tests/test_params.py`
- Modify: `infra/cdk/fde_cdk/stack.py`

**Interfaces:**
- Produces: `LaunchParams` dataclass-of-CfnParameters built by `add_launch_params(stack) -> LaunchParams` with attributes: `deploy_tier` (AllowedValues demo|production, Default demo), `model_provider` (AllowedValues bedrock|anthropic|openai|gemini|openai-compat, Default bedrock), `model_id` (Default ""), `provider_api_key` (NoEcho=True, Default ""), `compat_base_url` (Default ""), `embed_provider` (AllowedValues bedrock|openai|gemini|openai-compat, Default bedrock), `embed_model_id` (Default "amazon.titan-embed-text-v2:0"), `admin_email` (AllowedPattern for email, no default — required), `release_tag` (Default set by release.yml at synth time via the `FDE_RELEASE_TAG` env var, fallback "dev"), `assets_bucket` (Default "" → resolved from the region mapping when blank). Conditions: `is_production = CfnCondition(...)`, `is_bedrock_model`, `has_provider_key`. Mapping: `AssetsRegionMap` with `us-east-1 -> {"bucket": <from FDE_ASSETS_BUCKET env at synth, fallback "fde-platform-assets-us-east-1">}`.

- [ ] **Step 1: Failing tests** in `infra/cdk/tests/test_params.py` (import `synth_template` from `tests.test_synth`):

```python
from __future__ import annotations

from tests.test_synth import synth_template


def test_parameters_exist_with_safe_defaults() -> None:
    params = synth_template().to_json()["Parameters"]
    assert params["DeployTier"]["Default"] == "demo"
    assert set(params["DeployTier"]["AllowedValues"]) == {"demo", "production"}
    assert params["ModelProvider"]["Default"] == "bedrock"
    assert params["ProviderApiKey"]["NoEcho"] is True
    assert "Default" not in params["AdminEmail"]  # required, no default
    assert params["EmbedModelId"]["Default"] == "amazon.titan-embed-text-v2:0"


def test_admin_email_pattern_rejects_garbage() -> None:
    params = synth_template().to_json()["Parameters"]
    assert "@" in params["AdminEmail"]["AllowedPattern"]
```

- [ ] **Step 2: FAIL run.**
- [ ] **Step 3: Implement `params.py`** — a frozen `@dataclass class LaunchParams` holding the `cdk.CfnParameter`/`cdk.CfnCondition`/`cdk.CfnMapping` objects, one `add_launch_params(stack: cdk.Stack) -> LaunchParams` function creating them exactly as the Interfaces block specifies (logical IDs `DeployTier`, `ModelProvider`, `ModelId`, `ProviderApiKey`, `CompatBaseUrl`, `EmbedProvider`, `EmbedModelId`, `AdminEmail`, `ReleaseTag`, `AssetsBucket`). Wire into `FdePlatformStack.__init__` as `self.params = add_launch_params(self)`.
- [ ] **Step 4: PASS + cfn-lint + commit** `feat: launch parameters, conditions, region asset mapping`

---

### Task 3: Network + Database constructs

**Files:**
- Create: `infra/cdk/fde_cdk/network.py`, `infra/cdk/fde_cdk/database.py`, `infra/cdk/tests/test_network_db.py`
- Modify: `infra/cdk/fde_cdk/stack.py`

**Interfaces:**
- Consumes: `LaunchParams` (`deploy_tier`, `is_production` condition).
- Produces: `Network` construct exposing `.vpc: ec2.Vpc` (2 AZ, 1 NAT, private-with-egress subnets); `Database` construct exposing `.cluster: rds.DatabaseCluster`, `.secret: secretsmanager.ISecret` (RDS-managed, default rotation JSON shape), `.security_group`. Demo tier = Aurora Serverless v2 (`serverless_v2_min_capacity=2`, `max=8` — the min-ACU floor exists so HNSW stays inside shared_buffers, docs/09; comment this); production tier = provisioned `db.r6g.xlarge`. Engine: `rds.DatabaseClusterEngine.aurora_postgres(version=AuroraPostgresEngineVersion.VER_16_8)` — if the enum lacks 16_8, use `.of("16.8", "16")` and leave a comment citing docs/09's pgvector-0.8 floor. Database name `fde`. Deletion: `RemovalPolicy.SNAPSHOT` always; `deletion_protection` only under `is_production` (use the condition on the L1 override: `cluster.node.default_child.add_property_override(...)` pattern with `cdk.Fn.condition_if`).

- [ ] **Step 1: Failing tests:**

```python
from __future__ import annotations

from tests.test_synth import synth_template


def test_vpc_two_azs_one_nat() -> None:
    t = synth_template()
    t.resource_count_is("AWS::EC2::NatGateway", 1)
    assert len([k for k in t.to_json()["Resources"] if "PublicSubnet" in k and t.to_json()["Resources"][k]["Type"] == "AWS::EC2::Subnet"]) == 2


def test_aurora_engine_and_snapshot_policy() -> None:
    t = synth_template()
    clusters = t.find_resources("AWS::RDS::DBCluster")
    assert len(clusters) == 1
    cluster = next(iter(clusters.values()))
    assert cluster["Properties"]["Engine"] == "aurora-postgresql"
    assert cluster["Properties"]["EngineVersion"].startswith("16.")
    assert cluster["DeletionPolicy"] == "Snapshot"
    assert cluster["Properties"]["DatabaseName"] == "fde"


def test_db_secret_is_rds_managed_shape() -> None:
    t = synth_template()
    t.resource_count_is("AWS::SecretsManager::Secret", 1)  # provider key secret comes later; adjust in Task 6 if needed
```

- [ ] **Step 2: FAIL → implement → PASS.** Both constructs are plain `Construct` subclasses taking `(scope, id, *, params: LaunchParams, vpc=...)`. Wire in stack: `self.network = Network(self, "Network")`, `self.database = Database(self, "Database", params=self.params, vpc=self.network.vpc)`.
- [ ] **Step 3: cfn-lint + commit** `feat: network and Aurora pgvector database constructs`

---

### Task 4: Identity (Cognito) + IAM roles from the repo's policy templates

**Files:**
- Create: `infra/cdk/fde_cdk/identity.py`, `infra/cdk/fde_cdk/iam_roles.py`, `infra/cdk/tests/test_identity_iam.py`
- Modify: `infra/cdk/fde_cdk/stack.py`

**Interfaces:**
- Consumes: `LaunchParams.admin_email`.
- Produces: `Identity` construct: `.user_pool` (self-signup OFF, admin-create user seeded with `admin_email`), `.console_client` (auth-code flow, hosted UI domain `fde-<stackid-suffix>`), `.api_client`, `.m2m_client` (client-credentials grant, resource server scope `gateway/invoke`), `.discovery_url: str` (`https://cognito-idp.{region}.amazonaws.com/{pool_id}/.well-known/openid-configuration`). `IamRoles` construct: `.runtime_role` (×1 shared in v1 — one role for all three agent runtimes; least-privilege split is a documented follow-up), `.gate_role`, `.migration_role`, `.gateway_service_role`, `.memory_role`, each built by `_role_from_template(path, substitutions)` which loads the repo's JSON (e.g. `packages/fde-agents/src/fde_agents/deploy/iam/runtime-permissions-policy.json`), substitutes every `${VAR}` occurrence from a dict (`AWS_ACCOUNT_ID` → `cdk.Aws.ACCOUNT_ID`, `AWS_REGION` → `cdk.Aws.REGION`, `FDE_DB_SECRET_ARN` → the DB secret ARN token, `DB_RESOURCE_ID` → the cluster resource id token, `FDE_GATE_FUNCTION_NAME` → the gate function name), and parses into `iam.PolicyDocument.from_json`. Missing templates (Gateway service role, Memory role) get minimal inline policies written per the API calls in the repo's `gateway.py`/`memory.py` docstrings.

- [ ] **Step 1: Failing tests** (pool exists; self-signup off; 3 clients incl. one with client_credentials; roles reference the substituted secret ARN not a `${...}` literal):

```python
def test_cognito_pool_and_three_clients() -> None:
    t = synth_template()
    t.resource_count_is("AWS::Cognito::UserPool", 1)
    t.resource_count_is("AWS::Cognito::UserPoolClient", 3)


def test_no_unsubstituted_placeholders_in_any_role() -> None:
    import json
    raw = json.dumps(synth_template().to_json())
    assert "${AWS_ACCOUNT_ID}" not in raw
    assert "${FDE_DB_SECRET_ARN}" not in raw
    assert "${DB_RESOURCE_ID}" not in raw
```

- [ ] **Step 2: FAIL → implement → PASS.** `_role_from_template` reads the JSON with `Path(__file__).parents[3] / "packages" / ...` (repo-relative — infra/cdk lives in the repo; add a comment). Trust policies likewise from the `*-trust-policy.json` files.
- [ ] **Step 3: cfn-lint + commit** `feat: cognito identity and IAM roles rendered from repo policy templates`

---

### Task 5: Migration-runner Lambda (code + construct)

**Files:**
- Create: `infra/cdk/lambdas/migration_runner/handler.py`, `infra/cdk/lambdas/migration_runner/build.py`, `infra/cdk/fde_cdk/migrations.py`, `infra/cdk/tests/test_migration_runner.py`
- Modify: `infra/cdk/fde_cdk/stack.py`

**Interfaces:**
- Consumes: `Database.secret`, `Database.cluster`, `IamRoles.migration_role`, `LaunchParams` (`admin_email`, `assets_bucket`, `release_tag`).
- Produces: `Migrations` construct: a `lambda_.Function` (python3.12, arm64, 900s, VPC-attached, code `lambda_.Code.from_bucket(assets_bucket_ref, f"releases/{tag}/migration-runner.zip")`) + `cdk.CustomResource` (`service_token=fn.function_arn` via a `Provider`-free direct `AWS::CloudFormation::CustomResource` — the handler implements the cfn-response protocol itself to avoid the Provider framework's asset Lambdas, which would violate bootstrap-free) with properties `ReleaseTag` (so updates re-run) and `AdminEmail`. Handler contract (plain functions, unit-testable without AWS): `plan_migrations(applied: set[str], available: list[str]) -> list[str]` (forward-only, lexicographic, refuses gaps), `handler(event, context)` — on Create/Update: connect via secret (build DSN in the `_dsn_from_secrets_manager` JSON shape), ensure ledger table `ops.applied_migration(filename text primary key, applied_at timestamptz)`, apply pending `db/0*.sql` in order, create login users from generated passwords stored in new Secrets Manager secrets (`fde/db/agent`, `fde/db/gate`, `fde/db/ingest`) `IN ROLE fde_agent/fde_gate_service/fde_ingest`, insert the AdminEmail principal (read `db/004_hitl_gates.sql` for the principal table shape before writing this SQL), send SUCCESS/FAILED to the cfn response URL; on Delete: no-op SUCCESS (data outlives the stack per snapshot policy).
- `build.py`: zips `handler.py` + `db/*.sql` (copied from repo root at build time) + psycopg wheels for `aarch64-manylinux_2_28` (reuse the EXACT `uv pip install --target ... --python-platform aarch64-manylinux_2_28 --only-binary :all:` pattern from `packages/fde-gate/src/fde_gate/deploy/package.py` — read it first; that platform choice is a hard-won psycopg-binary fact). Output: `dist/migration-runner.zip`.

- [ ] **Step 1: Failing unit tests** for the pure logic (no AWS):

```python
from __future__ import annotations

import pytest

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "lambdas" / "migration_runner"))
from handler import plan_migrations  # noqa: E402


def test_plan_applies_only_pending_in_order() -> None:
    available = ["001_a.sql", "002_b.sql", "003_c.sql"]
    assert plan_migrations({"001_a.sql"}, available) == ["002_b.sql", "003_c.sql"]


def test_plan_refuses_gap() -> None:
    with pytest.raises(ValueError, match="002_b.sql"):
        plan_migrations({"001_a.sql", "003_c.sql"}, ["001_a.sql", "002_b.sql", "003_c.sql"])


def test_plan_noop_when_current() -> None:
    assert plan_migrations({"001_a.sql"}, ["001_a.sql"]) == []
```

Plus a synth test: custom resource exists, its Lambda's Code points at the assets bucket + `migration-runner.zip` key, and the function is VPC-attached.

- [ ] **Step 2: FAIL → implement handler + construct → PASS.** Handler stays stdlib+psycopg only; the cfn-response POST uses `urllib.request` (stdlib) — no `requests` dependency.
- [ ] **Step 3: Build check.** `uv run python lambdas/migration_runner/build.py && unzip -l dist/migration-runner.zip | head` — verify `handler.py`, `db/001*.sql`…`db/015*.sql` (or current highest), and `psycopg` are present.
- [ ] **Step 4: cfn-lint + commit** `feat: forward-only migration runner lambda + custom resource`

---

### Task 6: Gate service + MCP/embedder Fargate services

**Files:**
- Create: `infra/cdk/fde_cdk/gate.py`, `infra/cdk/fde_cdk/services.py`, `infra/cdk/tests/test_services.py`
- Modify: `infra/cdk/fde_cdk/stack.py`

**Interfaces:**
- Consumes: everything prior; `Identity.discovery_url`/`.api_client`; ECR Public base URI constant `ECR_PUBLIC_BASE = "public.ecr.aws/REPLACE_AT_RELEASE"` exposed from `params.py` as a Mapping value overridable via the `FDE_ECR_PUBLIC_ALIAS` env var at synth (release.yml sets it).
- Produces: `GateService`: Lambda (`fde-gate-service` semantics from `packages/fde-gate/src/fde_gate/deploy/lambda_fn.py` — python3.12, arm64, 900s, 512MB, handler `fde_gate.handler.lambda_handler`, VPC-attached, code from `releases/{tag}/fde-gate.zip`, env: `FDE_DB_SECRET_ARN`, `FDE_GATE_FUNCTION_NAME` (self), `FDE_SERVICE_NAME=fde-gate`, `FDE_MCP_URL` (the ALB), `FDE_RUNTIME_ARN_*` (from Task 7's agents), provider envs from params) + `apigatewayv2.HttpApi` with JWT authorizer (issuer = pool, audience = api client) on `ANY /{proxy+}` and unauthenticated `GET /healthz` + the two EventBridge rules (`rate(1 minute)` → `{"source":"fde.gate.tick"}`, `rate(1 hour)` → expiry — payload shapes from `packages/fde-gate/src/fde_gate/deploy/schedule.py`, read it). `Services`: one ECS cluster; `mcp_service` Fargate ARM64 (image `{ECR_PUBLIC_BASE}/fde-mcp:{tag}`, port 8080, env `FDE_MCP_TRANSPORT=http`, `FDE_DB_SECRET_ARN`, embed provider envs, `FDE_EMBED_API_KEY` via secret when `has_provider_key`) behind an **internal** ALB (`.mcp_url` output = `http://{alb.dns}/mcp`); `embedder_service` same task family, command `["fde-embedder"]`, role env `FDE_EMBEDDER_ROLE=fde_ingest`, DB secret = the ingest secret from Task 5.
- Note: cross-construct ordering — `GateService` needs the runtime ARNs that Task 7 creates; expose setter-free wiring by constructing `Agents` BEFORE `GateService` in the stack and passing `agents.runtime_arns: dict[str, str]` in. Adjust stack wiring order in Task 7.

- [ ] **Step 1: Failing tests** (gate Lambda handler string, healthz route without authorizer, exactly 2 EventBridge rules with the right schedule expressions, internal ALB scheme, MCP container image contains `/fde-mcp:`, embedder command `fde-embedder`):

```python
def test_gate_lambda_shape() -> None:
    t = synth_template()
    fns = t.find_resources("AWS::Lambda::Function")
    gate = [f for f in fns.values() if f["Properties"].get("Handler") == "fde_gate.handler.lambda_handler"]
    assert len(gate) == 1
    assert gate[0]["Properties"]["Architectures"] == ["arm64"]
    assert gate[0]["Properties"]["Timeout"] == 900


def test_alb_is_internal() -> None:
    t = synth_template()
    albs = t.find_resources("AWS::ElasticLoadBalancingV2::LoadBalancer")
    assert all(a["Properties"]["Scheme"] == "internal" for a in albs.values())


def test_two_gate_schedules() -> None:
    t = synth_template()
    rules = t.find_resources("AWS::Events::Rule")
    exprs = sorted(r["Properties"]["ScheduleExpression"] for r in rules.values())
    assert exprs == ["rate(1 hour)", "rate(1 minute)"]
```

- [ ] **Step 2: FAIL → implement → PASS** (read `lambda_fn.py`, `api.py`, `schedule.py` in fde-gate's deploy dir first — the CDK constructs mirror those exact shapes).
- [ ] **Step 3: cfn-lint + commit** `feat: gate lambda + http api + schedules; mcp and embedder fargate services`

---

### Task 7: AgentCore layer (runtimes, gateway, memory) + pull-through cache + outputs

**Files:**
- Create: `infra/cdk/fde_cdk/agents.py`, `infra/cdk/fde_cdk/outputs.py`, `infra/cdk/tests/test_agents_outputs.py`
- Modify: `infra/cdk/fde_cdk/stack.py`, `infra/cdk/fde_cdk/gate.py` (accept `runtime_arns`)

**Interfaces:**
- Consumes: `IamRoles.runtime_role/.gateway_service_role/.memory_role`, `Identity.discovery_url` + allowed audience, `Services.mcp_url`, params.
- Produces: `Agents` construct: `AWS::ECR::PullThroughCacheRule` (upstream `public.ecr.aws`, prefix `ecr-public`) + three AgentCore Runtimes (container artifact URI `{account}.dkr.ecr.{region}.amazonaws.com/ecr-public/{alias}/fde-{agent}:{tag}`, network PUBLIC, env per `packages/fde-agents/src/fde_agents/deploy/runtimes.py` `_environment_variables()` — READ IT — plus the provider envs/params and `FDE_MODEL_ID` baked for audit parity), one Gateway (MCP protocol, SEMANTIC search, CUSTOM_JWT with the Cognito discovery URL + audience, target = the MCP ALB endpoint per `gateway.py`), one Memory (three strategies per `memory.py`). Use `aws_bedrockagentcore` L2s where they cover it; L1 `Cfn*` otherwise — with a comment naming which docs page pinned each property. `.runtime_arns: dict[str, str]` (engagement/workflow/development → `attr` ARN tokens). `Outputs` construct: CfnOutputs `ReviewConsoleUrl` (the HTTP API URL), `CognitoLoginUrl` (hosted UI), `ApiEndpoint`, `McpEndpoint`, `FirstStepsUrl` (docs/13 GitHub URL).
- Stack wiring order becomes: Network → Database → Identity → IamRoles → Migrations → Services → Agents → GateService(runtime_arns=agents.runtime_arns) → Outputs.

- [ ] **Step 1: Failing tests** (3 runtimes exist — count the AgentCore runtime resource type; pull-through cache rule present; gateway has CUSTOM_JWT; 5 outputs present):

```python
def test_three_agentcore_runtimes_and_cache_rule() -> None:
    t = synth_template()
    j = t.to_json()["Resources"]
    runtimes = [r for r in j.values() if r["Type"].startswith("AWS::BedrockAgentCore") and "Runtime" in r["Type"] and "Endpoint" not in r["Type"]]
    assert len(runtimes) == 3
    t.resource_count_is("AWS::ECR::PullThroughCacheRule", 1)


def test_outputs_complete() -> None:
    outs = synth_template().to_json()["Outputs"]
    for key in ("ReviewConsoleUrl", "CognitoLoginUrl", "ApiEndpoint", "McpEndpoint", "FirstStepsUrl"):
        assert key in outs
```

- [ ] **Step 2: FAIL → implement → PASS.** Before writing each AgentCore construct, pull the current property reference via context7 (`aws-cdk-lib.aws_bedrockagentcore`) and cross-check field names against the boto3 payloads in `runtimes.py:131`, `gateway.py:83/119`, `memory.py:148` — the CFN properties mirror those APIs. Never guess a property name.
- [ ] **Step 3: Full suite + cfn-lint + commit** `feat: agentcore runtimes, gateway, memory via native CFN + stack outputs`

---

### Task 7.5: Ops layer — unconditional hygiene + a pluggable, replaceable ops package

**Added after a prod-ops design review (see ledger). Design stance (user-set): the ops package must be drop-in replaceable by the end user's own ops/health stack — the SNS topic is the integration seam; AWS-specific delivery (email, dashboard, budget) is default-on but cleanly detachable. Correctness hygiene is NOT optional and lives in the existing constructs.**

**Files:**
- Create: `infra/cdk/fde_cdk/ops.py`, `infra/cdk/tests/test_ops.py`
- Modify: `infra/cdk/fde_cdk/params.py` (add `OpsMode` allowed-values `email|topic-only|off` default `email`; `OpsAlertEmail` default "" → falls back to AdminEmail; `MonthlyBudgetUsd` default 300, 0 = no budget), `database.py`, `gate.py`, `services.py`, `migrations.py`, `app.py`, `infra/cdk/lambdas/migration_runner/handler.py`, `.github/workflows/ci.yml` only if lint codes change

**Part A — unconditional hygiene (existing constructs, not conditioned on OpsMode):**
- Explicit `logs.LogGroup` (retention ONE_MONTH, `RemovalPolicy.DESTROY`) passed via `log_group=` on the gate + migration Lambdas (**never `log_retention=`** — it synthesizes an asset Lambda and violates the bootstrap-free contract) and via `log_group=` inside both `aws_logs` drivers in services.py (kills the TWO_YEARS/RETAIN orphan defaults).
- One `sqs.Queue` `fde-ops-dlq` as `dead_letter_queue=` + `retry_attempts=2` on BOTH EventBridge rule targets in gate.py.
- `database.py`: `backup=rds.BackupProps(retention=cdk.Duration.days(7))`; `enable_data_api=True` (verify prop support for Serverless v2/PG16 in this lib version; if the L2 lacks it, L1 `EnableHttpEndpoint` override) — this is the break-glass path the Task-5 principal reconciliation requires.
- `migrations.py`/`handler.py`: watchdog — send FAILED to the cfn response URL when `context.get_remaining_time_in_millis() < 10_000` (thread timer or pre-check loop), so a timeout never hangs CloudFormation for an hour; unit-test the pure decision function.
- `app.py`: `cdk.Tags.of(app).add("app", "fde-platform")` + `add("stack-tier", ...)`.

**Part B — the pluggable ops package (`OpsLayer` construct in ops.py; every resource in it carries the `OpsEnabled` condition = OpsMode != "off"):**
- `sns.Topic` `fde-ops` — THE seam. Output `OpsTopicArn` always (docs/13: "subscribe Datadog/PagerDuty/your SIEM here; set OpsMode=topic-only to skip email"). Email subscription only under condition OpsMode == "email" (address = OpsAlertEmail if set else AdminEmail).
- Seven alarms, all → the topic (names/metrics/thresholds fixed): `FdeGateErrors` (gate Lambda Errors ≥3 over 3×1min), `FdeMigrationRunnerErrors` (≥1), `FdeMcpUnhealthyHosts` (≥1 for 3×1min), `FdeMcpTarget5xx` (≥5/5min), `FdeDbAcuCeiling` (ServerlessDatabaseCapacity avg ≥7.5 for 15min), `FdeDbConnections` (≥80% of the 2-ACU cap), `FdeEmbedQueueBacklog` (custom `FDE/Platform` `EmbedQueueDepth` ≥500 for 15min), plus `FdeOpsDlqMessages` (DLQ visible ≥1) — 8 total with the DLQ alarm.
- Embed-queue metric: EventBridge `rate(5 minutes)` rule → the migration-runner Lambda with input `{"source":"fde.ops.metrics"}`; handler branch runs `SELECT count(*) FROM kg.embed_queue WHERE completed_at IS NULL` and `PutMetricData`; add namespace-conditioned `cloudwatch:PutMetricData` to migration_role. Unit-test the branch dispatch.
- One `cloudwatch.Dashboard` `FdeOps` (5 rows: gate, MCP/ALB, embedder+queue depth, Aurora, migration runner) + Output `OpsDashboardUrl`.
- `AWS::Budgets::Budget` (condition: OpsMode != off AND MonthlyBudgetUsd != 0) notifying the same email.

**Tests (test_ops.py):** topic + 8 alarms exist and target the topic; every alarm's namespace/metric/threshold pinned; all OpsLayer resources carry the OpsEnabled condition; email subscription conditioned on the email mode; log groups exist with 30-day retention for all four services; both rules carry the DLQ; `enable_data_api`/`EnableHttpEndpoint` present; budget conditioned. Plus the hygiene assertions in existing test files where they fit better.

- [ ] Steps follow the established TDD cycle (failing tests → implement → gates → commit `feat: ops layer -- pluggable alarm package + unconditional hygiene`).

**Task 9 additions (absorb):** docs/13 gains: "Bring your own ops stack" section (topic seam, OpsMode matrix), SNS-subscription-confirmation first-step, CloudWatch Transaction Search manual enablement note, break-glass section (Data API query editor: the principal-reconciliation UPDATE, the embed-queue probe, `admin-set-user-password` for Cognito lockout), the docs/10 §4 compliance-channel retraction for launch-stack deployments, escalation-table → actual-surface mapping, deferred-by-name list (business-metrics dashboard, drift→SNS wiring, restore drill, per-runtime alarms, MCP /healthz route, orphaned-resource teardown sweep: fde/db/* secrets + any pre-7.5 service-created log groups + pull-through-cache repos).

**Task 10 additions (absorb):** acceptance also requires — kill the embedder task and break the DB-creds path → confirm the alarm email arrives; confirm the dashboard populates; perform the principal reconciliation via the Data API query editor; verify OpsMode=topic-only launch produces no email subscription.

---

### Task 8: Release pipeline + CI deploy-job fix

**Files:**
- Create: `.github/workflows/release.yml`
- Modify: `.github/workflows/ci.yml` (fix the broken deploy-job `runtimes --update` invocation)
- Create: `infra/cdk/RELEASING.md` (one-time setup: ECR Public alias creation, the 5 `aws ecr-public create-repository` commands, assets bucket creation `fde-platform-assets-us-east-1` with public-read bucket policy for `releases/*`, GitHub repo variables `FDE_ASSETS_BUCKET`, `FDE_ECR_PUBLIC_ALIAS`, secret `AWS_RELEASE_ROLE_ARN`)

**Interfaces:**
- Consumes: everything synthesizable; `packages/fde-gate`'s `fde-gate-deploy package`; Task 5's `build.py`.
- Produces: on tag `v*`: (1) OIDC-assume `AWS_RELEASE_ROLE_ARN`; (2) `docker buildx` push of the 5 ARM64 images to `public.ecr.aws/$FDE_ECR_PUBLIC_ALIAS/fde-{mcp,engagement,workflow,development,sor}:$TAG` (reuse the matrix from ci.yml's `images` job, now with `push: true` and ECR Public login: `aws ecr-public get-login-password --region us-east-1 | docker login --username AWS --password-stdin public.ecr.aws`); (3) `uv run fde-gate-deploy package --out dist/fde-gate.zip` and `cd infra/cdk && uv run python lambdas/migration_runner/build.py`; (4) `FDE_RELEASE_TAG=$TAG FDE_ASSETS_BUCKET=... FDE_ECR_PUBLIC_ALIAS=... uv run python app.py` then upload `cdk.out/FdePlatform.template.json`, both zips → `s3://$FDE_ASSETS_BUCKET/releases/$TAG/`; (5) job summary printing the launch URL: `https://console.aws.amazon.com/cloudformation/home?region=us-east-1#/stacks/create/review?templateURL=https://$FDE_ASSETS_BUCKET.s3.amazonaws.com/releases/$TAG/FdePlatform.template.json&stackName=fde-platform`.
- CI deploy-job fix: replace the rejected `fde-agents-deploy runtimes --update --agents engagement workflow development ...` step with three per-agent invocations `--update "$FDE_SMOKE_RUNTIME_ID_<AGENT>"` behind the same secrets gate — read `runtimes.py:161` (`update_runtime`) and its argparse to get the exact flag shape right; the fix must be consistent with what `runtimes.py` actually accepts today, NOT a change to runtimes.py.

- [ ] **Step 1:** Write `release.yml` per the produces-block (no test harness for workflows — validate with `actionlint` if available, else careful review; run `uvx actionlint` and fix findings).
- [ ] **Step 2:** Fix the ci.yml deploy job; `uvx actionlint` both workflows.
- [ ] **Step 3:** Write `RELEASING.md` with every one-time command spelled out.
- [ ] **Step 4: Commit** `feat: tag-triggered release pipeline (ECR Public + assets bucket + hosted template); fix deploy-job update invocation`

---

### Task 9: docs/13-launch-stack.md + README (button held) + PR

**Files:**
- Create: `docs/13-launch-stack.md`
- Modify: `README.md`, `CLAUDE.md` (one Commands line: `cd infra/cdk && uv run pytest tests && uv run python app.py  # synth the launch stack (docs/13)`), spec status line → implemented
- PR via `gh pr create`

**Content requirements for docs/13 (no placeholders):** what the button deploys (the §-by-§ resource list); the parameter table from the spec verbatim; first steps after launch (Cognito login → review console → run an agent task through the Gateway → merge → search); teardown (`delete-stack`, snapshot note); cost expectations marked "estimated — live-validated numbers land after the first real launch"; **Operationalizing the full platform**: when to add SoR adapters (`fde-sor deploy` serverless path or `infra/k8s`), the training pipeline (docs/06), hardening checklist pointer (docs/10 + docs/09 §checklist: Guardrails, Transaction Search, backup/PITR, per-agent role split — name the v1 shared runtime role explicitly as a hardening TODO); honesty label: "the stack is written against verified API shapes and CI-linted; not yet live-validated — the README button appears only after a real launch."
**README:** inside the existing deploy-oriented details block, add the Launch-Stack button as an HTML comment (`<!-- LAUNCH BUTTON (enable after live validation, Task 10): [![Launch Stack](...)](console URL) -->`) plus one visible line pointing to docs/13. Do not touch pinned strings (docs/06 weights, 0.78s, CI smoke-count step name).

- [ ] **Step 1:** Write docs/13; README + CLAUDE.md edits; spec status update.
- [ ] **Step 2: Gates.** `cd infra/cdk && uv run pytest tests && uv run python app.py && uv run cfn-lint cdk.out/FdePlatform.template.json && uv run ruff check .`; workspace untouched-check: `uv run pytest packages` still green from repo root; `git diff --stat` shows no `packages/` or `db/` changes.
- [ ] **Step 3: Push + PR** onto `main` (or stacked onto PR #1's branch if it hasn't merged — state which in the PR body): title `feat: one-click AWS deploy — CDK launch stack, release pipeline, docs (button held pending live validation)`. Body: resource inventory, parameter table, what is validated (synth+lint+unit) vs not (live AWS), the Task 10 gate, RELEASING.md one-time setup summary, note asking the user to add `cdk-lint` to required checks after merge. End with the standard attribution line. **Stop after opening the PR — the user merges.**

---

### Task 10: LIVE-TEST GATE (requires the user — do not start without them)

**This task cannot run without the user:** AWS credentials, the one-time RELEASING.md setup (ECR Public alias, assets bucket, release role), and a cut tag. It is the acceptance test for everything above.

- [ ] **Step 1:** User completes RELEASING.md one-time setup; cut a pre-release tag (`v0.3.0-rc1`) to exercise release.yml end-to-end; fix pipeline failures as they surface.
- [ ] **Step 2:** Launch the stack from the emitted console URL in the user's account (demo tier, bedrock provider first). Iterate on failures — budget multiple create/delete cycles; each fix is a normal commit + re-tag.
- [ ] **Step 3:** Acceptance: Cognito login → review console loads → run one engagement task through the Gateway (`fde-agents-deploy invoke` against the deployed runtime, or the gate's workflow-run path) → proposal → approve → merged commit visible → `kg_search` returns fused results (docs/09 phase-2 done-when, now on live AWS). Then test a non-bedrock provider launch (anthropic + openai embeddings) to validate the provider parameters.
- [ ] **Step 4:** Record real costs + timings in docs/13 (replace "estimated"); flip honesty labels to "live-validated <date>, us-east-1"; uncomment the README button; teardown test: `delete-stack` completes clean, snapshot exists.
- [ ] **Step 5:** Final commit + push to the PR (or a follow-up PR if already merged). **User merges.**

---

## Self-review notes (already applied)

- Spec coverage: §1→T1, §2→T2, §3→T3-5, §4→T6-7, §5→T8, §6→T9-10; all five spec decisions and all non-goals respected (no SoR/RFT resources, no CLI, single region documented).
- Bootstrap-free contract is enforced mechanically (BootstraplessSynthesizer + `test_no_cdk_assets_anywhere`), not by convention.
- Type consistency: `LaunchParams` attribute names used in T3-T7 match T2's definition; `synth_template()` helper defined once in T1 and imported everywhere; `runtime_arns: dict[str,str]` produced in T7 = consumed in T6's gate env (wiring order fixed in T7).
- Known unknowns are pinned as verify-at-implementation steps with sources (AgentCore CFN property names ← context7 + repo deploy modules; Aurora 16.8 enum; pull-through-cache for AgentCore images), never silently assumed.
