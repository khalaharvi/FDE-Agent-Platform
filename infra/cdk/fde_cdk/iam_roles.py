"""IAM roles rendered from the repo's own checked-in policy JSON.

`packages/fde-agents/src/fde_agents/deploy/iam/*.json` and
`packages/fde-gate/src/fde_gate/deploy/iam/*.json` are the templates a
human operator would otherwise hand-render once per resource with their own
tooling (see each file's own `_comment`). This construct is that renderer,
run at `cdk synth` time instead: it loads each file's text, replaces every
`${VAR}` occurrence with a CDK token (`cdk.Aws.ACCOUNT_ID`, a secret ARN
token, ...), and parses the result into `iam.PolicyDocument`/principal
objects. The templates stay the single source of truth -- the *exact* JSON
`fde_agents/deploy/runtimes.py` and `fde_gate/deploy/lambda_fn.py` would
render by hand for a non-CDK deploy is what ends up in the CloudFormation
template too, not a hand-rebuilt CDK approximation that could drift from
it.

Why `str.replace` in a loop, not `string.Template.substitute`: CDK tokens
are themselves strings shaped like `${Token[TOKEN.123]}` -- once a
substitution value (e.g. `cdk.Aws.ACCOUNT_ID`) has been spliced into the
template text, the text contains a token's own `${...}` syntax alongside
whatever `${OTHER_VAR}` placeholders haven't been substituted yet.
`string.Template.substitute` re-parses its input for `$`-prefixed patterns
on every call and does not tolerate `$` characters it doesn't recognize as
one of ITS OWN placeholders (it raises `ValueError: Invalid placeholder`
the moment it meets a token's `${Token[...]}` shape, which is exactly the
literal `$` syntax CDK tokens always use). Plain `str.replace(f"${{{VAR}}}",
value)` never re-scans -- it only looks for the exact literal substring
being replaced -- so a token already sitting in the text from an earlier
substitution is inert to every later one.

v1 ships ONE shared `runtime_role` for all three agent runtimes (Engagement,
Workflow, Development) rather than the least-privilege ideal of one role
per runtime. `runtime-trust-policy.json`'s own `_comment` says as much
("in the least-privilege case, its own role") -- splitting this out is a
documented follow-up, not an oversight; the trade today is one shared
`AGENT_RUNTIME_ARN` condition scoped to a wildcard over the account's
AgentCore runtimes rather than three roles each scoped to its own runtime
ARN (which would require the runtime to already exist before its own role
could be created -- see the `AGENT_RUNTIME_ARN` substitution below for the
circularity this breaks and how).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import aws_cdk as cdk
import aws_cdk.aws_iam as iam
import aws_cdk.aws_rds as rds
import aws_cdk.aws_secretsmanager as secretsmanager
from constructs import Construct

# Fixed NOW, not derived, because Task 6's `GateService` construct (the
# actual `AWS::Lambda::Function`) and this task's `gate_role` (whose
# permissions policy scopes a CloudWatch log group and a self-invoke
# `lambda:InvokeFunction` to this exact name) are built by DIFFERENT
# constructs that never see each other's Python objects -- there is no CDK
# token to share between them the way `database.secret.secret_arn` is
# shared, because the log-group/self-invoke ARNs need to be correct at
# ROLE-CREATION time, before the function exists. Task 6 must construct its
# `lambda_.Function` with `function_name=GATE_FUNCTION_NAME` verbatim (not
# a fresh literal, not CDK's auto-generated name) or this role's
# `OwnLogGroupOnly` / `AsyncSelfInvoke` statements silently stop matching
# the real function's ARN.
GATE_FUNCTION_NAME = "fde-gate-service"

# `infra/cdk/fde_cdk/iam_roles.py` -> parents[0]=fde_cdk, [1]=cdk, [2]=infra,
# [3]=repo root. infra/cdk is not a workspace member (its own uv project,
# see infra/cdk/README.md) but it DOES live inside the same repo checkout,
# so a repo-relative path from `__file__` is stable in dev, CI, and the
# release build alike -- there is no installed "fde_cdk" wheel to resolve
# these templates through `importlib.resources` the way `gateway.py` does
# for its own package data.
_REPO_ROOT = Path(__file__).resolve().parents[3]
_AGENTS_IAM_DIR = _REPO_ROOT / "packages" / "fde-agents" / "src" / "fde_agents" / "deploy" / "iam"
_GATE_IAM_DIR = _REPO_ROOT / "packages" / "fde-gate" / "src" / "fde_gate" / "deploy" / "iam"

_RUNTIME_TRUST_POLICY = _AGENTS_IAM_DIR / "runtime-trust-policy.json"
_RUNTIME_PERMISSIONS_POLICY = _AGENTS_IAM_DIR / "runtime-permissions-policy.json"
_GATE_TRUST_POLICY = _GATE_IAM_DIR / "gate-lambda-trust-policy.json"
_GATE_PERMISSIONS_POLICY = _GATE_IAM_DIR / "gate-lambda-permissions-policy.json"


def _role_from_template(path: Path, substitutions: dict[str, str]) -> dict[str, Any]:
    """Load `path`'s JSON text, substitute every `${VAR}` occurrence from
    `substitutions` via plain `str.replace` (see module docstring for why
    not `string.Template`), and return the parsed dict. Callers turn this
    into either an `iam.PolicyDocument` (`.from_json`, for a permissions
    policy) or a trust principal (`_principal_from_trust_document`, for an
    `AssumeRolePolicyDocument`) -- both a repo template can describe, so
    this function stays one level below either interpretation.

    Substituting every entry in `substitutions` against every template
    (rather than a bespoke subset per file) is deliberate belt-and-braces:
    a template that doesn't mention a given `${VAR}` is simply untouched by
    that entry, and `test_no_unsubstituted_placeholders_in_any_role` only
    names a subset of vars explicitly (the brief's own three, plus the ones
    this module added) -- being exhaustive here means a placeholder the
    test doesn't happen to name yet still can't leak through unsubstituted.
    """
    text = path.read_text(encoding="utf-8")
    for var, value in substitutions.items():
        text = text.replace(f"${{{var}}}", value)
    doc: dict[str, Any] = json.loads(text)
    return doc


def _permissions_policy_from_template(
    path: Path, substitutions: dict[str, str]
) -> iam.PolicyDocument:
    return iam.PolicyDocument.from_json(_role_from_template(path, substitutions))


def _principal_from_trust_document(doc: dict[str, Any]) -> iam.IPrincipal:
    """Both trust-policy templates in this repo carry exactly one Statement
    with a single `Service` principal and a `Condition` block (see each
    file's own `_comment`) -- reduce that one statement to an
    `iam.ServicePrincipal` carrying the SAME Condition block CloudFormation
    would have emitted from the raw JSON, so the synthesized
    `AssumeRolePolicyDocument` is what the template's author wrote, not a
    hand-rebuilt approximation of it."""
    statement = doc["Statement"][0]
    service = statement["Principal"]["Service"]
    conditions = statement.get("Condition")
    return iam.ServicePrincipal(service, conditions=conditions)


class IamRoles(Construct):
    """Every execution role the launch-button stack's compute needs, each
    either rendered from one of the repo's own checked-in IAM policy JSON
    templates (`runtime_role`, `gate_role`) or a minimal inline policy
    derived from the API calls the corresponding deploy script makes
    (`gateway_service_role`, `memory_role`, `migration_role` -- none of
    those three have a repo template to render from; see each role's own
    comment below for its derivation)."""

    runtime_role: iam.Role
    gate_role: iam.Role
    migration_role: iam.Role
    gateway_service_role: iam.Role
    memory_role: iam.Role

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        db_secret: secretsmanager.ISecret,
        db_cluster: rds.DatabaseCluster,
    ) -> None:
        super().__init__(scope, construct_id)

        # One substitution dict, applied (harmlessly) to every template --
        # see `_role_from_template`'s docstring for why over-substituting is
        # the safe default here.
        substitutions = {
            "AWS_ACCOUNT_ID": cdk.Aws.ACCOUNT_ID,
            "AWS_REGION": cdk.Aws.REGION,
            "FDE_DB_SECRET_ARN": db_secret.secret_arn,
            "DB_RESOURCE_ID": db_cluster.cluster_resource_identifier,
            "FDE_GATE_FUNCTION_NAME": GATE_FUNCTION_NAME,
            # The trust policy's own `_comment` names the circularity this
            # breaks: the three AgentCore runtimes (Task 7) do not exist
            # yet when this role is created (roles are created once, up
            # front; runtimes reference the role, not the other way
            # around), so no single, already-known runtime ARN can be
            # substituted here. An account-scoped wildcard over every
            # `bedrock-agentcore` runtime in THIS account is how the
            # template's own trust condition (`aws:SourceArn` `ArnLike`)
            # gets satisfied without that forward reference -- paired with
            # the SAME statement's `aws:SourceAccount` condition, this still
            # rules out a runtime in a DIFFERENT account assuming the role,
            # same as the runtime-scoped version would; what it does NOT
            # rule out is a different runtime in THIS SAME account (e.g. an
            # unrelated experiment someone else deployed) assuming a role
            # meant only for this platform's three agents. That's the
            # least-privilege gap the template's own comment already
            # documents as a hardening follow-up (tracked for docs/13) --
            # matching v1's single shared `runtime_role` for all three
            # agents in the first place.
            "AGENT_RUNTIME_ARN": cdk.Fn.join(
                "",
                [
                    "arn:aws:bedrock-agentcore:",
                    cdk.Aws.REGION,
                    ":",
                    cdk.Aws.ACCOUNT_ID,
                    ":runtime/*",
                ],
            ),
        }

        self.runtime_role = iam.Role(
            self,
            "RuntimeRole",
            description=(
                "Shared execution role for all three FDE AgentCore runtimes "
                "(v1: not split per-runtime, see module docstring)."
            ),
            assumed_by=_principal_from_trust_document(
                _role_from_template(_RUNTIME_TRUST_POLICY, substitutions)
            ),
            inline_policies={
                "runtime-permissions": _permissions_policy_from_template(
                    _RUNTIME_PERMISSIONS_POLICY, substitutions
                )
            },
        )

        self.gate_role = iam.Role(
            self,
            "GateRole",
            description=(
                "Execution role for the fde-gate-service Lambda "
                "(review console, merge, workflow publish/run)."
            ),
            assumed_by=_principal_from_trust_document(
                _role_from_template(_GATE_TRUST_POLICY, substitutions)
            ),
            inline_policies={
                "gate-permissions": _permissions_policy_from_template(
                    _GATE_PERMISSIONS_POLICY, substitutions
                )
            },
        )

        # --- Migration role: no repo template (this role predates any
        # deploy script -- Task 5 writes the Lambda that assumes it). VPC-
        # Lambda basics come from the AWS-managed policy every VPC-attached
        # Lambda needs (ENI create/describe/delete, matching the identical
        # hand-written statement in gate-lambda-permissions-policy.json's
        # own `VpcNetworkInterfacesForPrivateDb`, just via the managed
        # policy instead of a hand-copied statement since this role has no
        # OTHER reason to hand-roll one). The two Secrets Manager
        # statements are this role's actual job per the brief: read the
        # cluster's master secret to connect, then mint the three
        # login-user secrets (`fde/db/agent`, `fde/db/gate`, `fde/db/
        # ingest`, per Task 5's brief) that the migration handler creates
        # `IN ROLE fde_agent/fde_gate_service/fde_ingest`.
        self.migration_role = iam.Role(
            self,
            "MigrationRole",
            description="Execution role for the forward-only migration-runner Lambda (Task 5).",
            assumed_by=iam.ServicePrincipal(
                "lambda.amazonaws.com",
                conditions={"StringEquals": {"aws:SourceAccount": cdk.Aws.ACCOUNT_ID}},
            ),
            managed_policies=[
                iam.ManagedPolicy.from_aws_managed_policy_name(
                    "service-role/AWSLambdaVPCAccessExecutionRole"
                )
            ],
            inline_policies={
                "migration-permissions": iam.PolicyDocument(
                    statements=[
                        iam.PolicyStatement(
                            sid="ReadTheDbSecret",
                            effect=iam.Effect.ALLOW,
                            actions=["secretsmanager:GetSecretValue"],
                            resources=[db_secret.secret_arn],
                        ),
                        iam.PolicyStatement(
                            sid="MintLoginUserSecrets",
                            effect=iam.Effect.ALLOW,
                            actions=[
                                "secretsmanager:CreateSecret",
                                "secretsmanager:PutSecretValue",
                                "secretsmanager:TagResource",
                            ],
                            resources=[
                                cdk.Fn.join(
                                    "",
                                    [
                                        "arn:aws:secretsmanager:",
                                        cdk.Aws.REGION,
                                        ":",
                                        cdk.Aws.ACCOUNT_ID,
                                        ":secret:fde/db/*",
                                    ],
                                )
                            ],
                        ),
                    ]
                )
            },
        )

        # --- Gateway service role: no repo template. Derived from the two
        # AWS API calls `fde_agents/deploy/gateway.py` makes that need this
        # role's OWN permissions (not the caller's) to succeed:
        #   1. `create_gateway_target(...credentialProviderConfigurations=
        #      [{"credentialProviderType": "GATEWAY_IAM_ROLE"}])` is set on
        #      BOTH targets it creates (`create_mcp_server_target` line
        #      139-141, `create_lambda_target` line 171-173) -- that
        #      credential-provider type means the Gateway invokes each
        #      target USING THIS ROLE's own credentials, not the caller's.
        #      For the `lambda` target that is a literal `lambda:
        #      InvokeFunction` on the batch Lambda; scoped to `fde-*`
        #      function names (mirroring gate-lambda-permissions-policy.
        #      json's own `fde-*`-scoped statements) since the specific
        #      batch Lambda's ARN isn't known to this construct (it is
        #      created by a later, unrelated deploy step, not this stack).
        #      The `mcpServer` target is an internal-ALB HTTP endpoint
        #      (Task 6), not an IAM-actioned resource, so it needs no
        #      additional statement here.
        #   2. `create_gateway(...authorizerType="CUSTOM_JWT"...)` (line
        #      101-108) is what makes the Gateway a workload-identity
        #      participant in the same sense the runtime role already is
        #      (`runtime-permissions-policy.json`'s
        #      `WorkloadIdentityTokenForGatewayAndMemory` statement) --
        #      reusing that exact action here.
        self.gateway_service_role = iam.Role(
            self,
            "GatewayServiceRole",
            description=(
                "Execution role for the AgentCore Gateway fronting the FDE "
                "knowledge-graph MCP server (fde_agents/deploy/gateway.py)."
            ),
            assumed_by=iam.ServicePrincipal(
                "bedrock-agentcore.amazonaws.com",
                conditions={"StringEquals": {"aws:SourceAccount": cdk.Aws.ACCOUNT_ID}},
            ),
            inline_policies={
                "gateway-service-permissions": iam.PolicyDocument(
                    statements=[
                        iam.PolicyStatement(
                            sid="InvokeLambdaBatchTarget",
                            effect=iam.Effect.ALLOW,
                            actions=["lambda:InvokeFunction"],
                            resources=[
                                cdk.Fn.join(
                                    "",
                                    [
                                        "arn:aws:lambda:",
                                        cdk.Aws.REGION,
                                        ":",
                                        cdk.Aws.ACCOUNT_ID,
                                        ":function:fde-*",
                                    ],
                                )
                            ],
                        ),
                        iam.PolicyStatement(
                            sid="WorkloadIdentityForJwtExchange",
                            effect=iam.Effect.ALLOW,
                            actions=["bedrock-agentcore:GetWorkloadAccessToken*"],
                            resources=["*"],
                        ),
                    ]
                )
            },
        )

        # --- Memory role: no repo template. Derived from
        # `fde_agents/deploy/memory.py`'s `create_memory(...
        # memoryExecutionRoleArn=args.execution_role_arn...)` (line 156-162)
        # and the three strategies it always provisions (`_semantic_strategy`
        # / `_summary_strategy` / `_user_preference_strategy`, lines 76-117):
        # each is a `*MemoryStrategy` that extracts durable facts/summaries/
        # preferences FROM raw conversation turns, which is model
        # inference AgentCore Memory runs using this role's credentials --
        # so it needs the same `bedrock:InvokeModel*` shape the runtime role
        # already has (`runtime-permissions-policy.json`'s
        # `InvokeTheModel` statement, reused verbatim here) plus its own
        # CloudWatch Logs group to write extraction-job logs to, mirroring
        # `runtime-permissions-policy.json`'s `RuntimeLogs` statement but
        # under the memory (not runtime) log-group prefix.
        self.memory_role = iam.Role(
            self,
            "MemoryRole",
            description=(
                "Execution role for AgentCore Memory's semantic/summary/"
                "user-preference extraction strategies (fde_agents/deploy/memory.py)."
            ),
            assumed_by=iam.ServicePrincipal(
                "bedrock-agentcore.amazonaws.com",
                conditions={"StringEquals": {"aws:SourceAccount": cdk.Aws.ACCOUNT_ID}},
            ),
            inline_policies={
                "memory-execution-permissions": iam.PolicyDocument(
                    statements=[
                        iam.PolicyStatement(
                            sid="InvokeModelForMemoryExtraction",
                            effect=iam.Effect.ALLOW,
                            actions=[
                                "bedrock:InvokeModel",
                                "bedrock:InvokeModelWithResponseStream",
                            ],
                            resources=[
                                cdk.Fn.join(
                                    "", ["arn:aws:bedrock:", cdk.Aws.REGION, "::foundation-model/*"]
                                ),
                                cdk.Fn.join(
                                    "",
                                    [
                                        "arn:aws:bedrock:",
                                        cdk.Aws.REGION,
                                        ":",
                                        cdk.Aws.ACCOUNT_ID,
                                        ":inference-profile/*",
                                    ],
                                ),
                            ],
                        ),
                        iam.PolicyStatement(
                            sid="MemoryExtractionLogs",
                            effect=iam.Effect.ALLOW,
                            actions=[
                                "logs:CreateLogGroup",
                                "logs:CreateLogStream",
                                "logs:PutLogEvents",
                            ],
                            resources=[
                                cdk.Fn.join(
                                    "",
                                    [
                                        "arn:aws:logs:",
                                        cdk.Aws.REGION,
                                        ":",
                                        cdk.Aws.ACCOUNT_ID,
                                        ":log-group:/aws/bedrock-agentcore/memory/*",
                                    ],
                                )
                            ],
                        ),
                    ]
                )
            },
        )
