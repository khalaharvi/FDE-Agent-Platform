# Releasing

One-time AWS + GitHub setup for `.github/workflows/release.yml` — the
tag-triggered pipeline that is the Launch button's supply chain. Once this
setup exists, pushing a tag matching `v*` is the entire release process:
the workflow builds and pushes the 5 ARM64 images to ECR Public, packages
the two Lambda zips, synthesizes the launch template pinned to that tag,
and uploads all four artifacts to
`s3://$FDE_ASSETS_BUCKET/releases/$TAG/`. Nothing below has been run
against live AWS (see the repo's "AWS honesty rule" in `CLAUDE.md`/README)
— these are the exact commands and JSON shapes the workflow assumes exist,
not a verified transcript.

Run everything in this doc once per AWS account, in `us-east-1` (the
region the assets bucket, the hosted template's launch region, and ECR
Public's own auth endpoint are all pinned to for v1 — see
`fde_cdk/params.py`'s `_DEFAULT_ASSETS_BUCKET` comment).

## 1. ECR Public repositories

ECR Public (`public.ecr.aws`) is a single global registry per account, not
a per-region resource — one **alias** for the account, then one repository
per image name underneath it. If this account has never used ECR Public
before, create the alias first (console: ECR → Public → "Get started",
or `aws ecr-public create-registry-alias` where supported by your account
type) and note the alias string; it is the value of `FDE_ECR_PUBLIC_ALIAS`
in step 3.

Then create the 5 repositories the release workflow pushes to — the names
must match `release.yml`'s matrix (`fde-mcp`, `fde-engagement`,
`fde-workflow`, `fde-development`, `fde-sor`) exactly:

```bash
for repo in fde-mcp fde-engagement fde-workflow fde-development fde-sor; do
  aws ecr-public create-repository --repository-name "$repo" --region us-east-1
done
```

`--region us-east-1` is required even if you never otherwise use that
region: ECR Public's control-plane API and its `get-login-password` auth
endpoint both live only in `us-east-1`, regardless of where any other
resource in this stack runs.

## 2. Assets bucket

Create the bucket that holds every release's template + zips, in
`us-east-1`:

```bash
aws s3api create-bucket --bucket fde-platform-assets-us-east-1 --region us-east-1
```

(`us-east-1` is the one region where `create-bucket` takes no
`LocationConstraint` — for any other region you'd need
`--create-bucket-configuration LocationConstraint=<region>`, moot here
since v1 is `us-east-1`-only.)

The CloudFormation "Launch Stack" flow needs anonymous, unauthenticated
`GET` on everything under `releases/` (the template itself, fetched by
the CloudFormation console over plain HTTPS with no AWS credentials) —
attach this bucket policy:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "PublicReadReleases",
      "Effect": "Allow",
      "Principal": "*",
      "Action": "s3:GetObject",
      "Resource": "arn:aws:s3:::fde-platform-assets-us-east-1/releases/*"
    }
  ]
}
```

```bash
aws s3api put-bucket-policy \
  --bucket fde-platform-assets-us-east-1 \
  --policy file://bucket-policy.json
```

This bucket also has **S3 Block Public Access** settings that override a
bucket policy by default on newly created buckets — the policy above will
be rejected (or silently ineffective) until the four "block public
access" settings are turned off for this bucket:

```bash
aws s3api put-public-access-block \
  --bucket fde-platform-assets-us-east-1 \
  --public-access-block-configuration \
  BlockPublicAcls=false,IgnorePublicAcls=false,BlockPublicPolicy=false,RestrictPublicBuckets=false
```

## 3. GitHub OIDC role for the release workflow

`release.yml` assumes an IAM role via GitHub's OIDC provider — the same
mechanism `ci.yml`'s existing `deploy` job uses with
`secrets.AWS_DEPLOY_ROLE_ARN`, just a distinct role/secret so the
tag-triggered release path and the main-branch deploy path stay
independently revocable. If this account has never configured GitHub
OIDC before (i.e. `AWS_DEPLOY_ROLE_ARN`'s role was not set up this way
either), create the provider once:

```bash
aws iam create-open-id-connect-provider \
  --url https://token.actions.githubusercontent.com \
  --client-id-list sts.amazonaws.com \
  --thumbprint-list 6938fd4d98bab03faadb97b34396831e3780aea1
```

(That thumbprint is GitHub's well-known OIDC root CA thumbprint,
published in GitHub's own OIDC docs; AWS also accepts OIDC providers
without a thumbprint list — you may omit `--thumbprint-list` on newer
`aws-cli`/IAM behavior, which fetches it automatically.)

Trust policy for the release role — scoped to this repo's **tag** refs
only (not branches, not PRs), since that is the only thing that should be
able to push production images and a public template:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Principal": {
        "Federated": "arn:aws:iam::<ACCOUNT_ID>:oidc-provider/token.actions.githubusercontent.com"
      },
      "Action": "sts:AssumeRoleWithWebIdentity",
      "Condition": {
        "StringEquals": {
          "token.actions.githubusercontent.com:aud": "sts.amazonaws.com"
        },
        "StringLike": {
          "token.actions.githubusercontent.com:sub": "repo:khalaharvi/FDE-Agent-Platform:ref:refs/tags/v*"
        }
      }
    }
  ]
}
```

Permissions the role needs (attach as an inline or managed policy — scope
tightened to the two resources this workflow touches):

- ECR Public: `ecr-public:GetAuthorizationToken`, `sts:GetServiceBearerToken`
  (required alongside the ECR Public token call), and
  `ecr-public:BatchCheckLayerAvailability`, `ecr-public:PutImage`,
  `ecr-public:InitiateLayerUpload`, `ecr-public:UploadLayerPart`,
  `ecr-public:CompleteLayerUpload` scoped to the 5 repository ARNs from
  step 1.
- S3: `s3:PutObject` (and `s3:PutObjectAcl` if the bucket's ownership
  settings require it) on
  `arn:aws:s3:::fde-platform-assets-us-east-1/releases/*`.

```bash
aws iam create-role \
  --role-name fde-release-pipeline \
  --assume-role-policy-document file://release-trust-policy.json
```

Record the resulting role ARN — it is `secrets.AWS_RELEASE_ROLE_ARN` in
step 4.

## 4. GitHub repo variables and secret

Set these once (`gh` shown; the same names work via Settings → Secrets
and variables → Actions in the GitHub UI):

```bash
gh variable set FDE_ASSETS_BUCKET     --body "fde-platform-assets-us-east-1"
gh variable set FDE_ECR_PUBLIC_ALIAS  --body "<the alias from step 1>"
gh secret   set AWS_RELEASE_ROLE_ARN  --body "<the role ARN from step 3>"
```

| Name | Kind | Consumed by |
|---|---|---|
| `FDE_ASSETS_BUCKET` | repo variable | `release.yml` (S3 upload prefix, `FDE_ASSETS_BUCKET` env for `app.py` synth); `fde_cdk/params.py`'s `AssetsBucket` parameter default |
| `FDE_ECR_PUBLIC_ALIAS` | repo variable | `release.yml` (image push tags, `FDE_ECR_PUBLIC_ALIAS` env for `app.py` synth); `fde_cdk/params.py`'s `ECR_PUBLIC_ALIAS` |
| `AWS_RELEASE_ROLE_ARN` | repo secret | `release.yml`'s `aws-actions/configure-aws-credentials` step in both the `images` and `assets` jobs |

## 5. Runtime IDs for `ci.yml`'s `deploy` job

Separately from the release pipeline above, `ci.yml`'s existing `deploy`
job (main-branch / `workflow_dispatch`, gated behind the `production`
environment) updates three already-provisioned AgentCore runtimes via
`fde-agents-deploy runtimes --agents <name> --update <AGENT_RUNTIME_ID>`.
`runtimes.py`'s `--update` takes the runtime's ID as its value and its
`update_requires_single_agent` guard rejects more than one `--agents`
value per invocation — hence three separate steps, each needing its own
runtime's ID. These three repo variables supply them:

| Name | Value |
|---|---|
| `FDE_RUNTIME_ID_ENGAGEMENT` | the `agentRuntimeId` of the already-created `fde-engagement-agent` runtime |
| `FDE_RUNTIME_ID_WORKFLOW` | the `agentRuntimeId` of the already-created `fde-workflow-agent` runtime |
| `FDE_RUNTIME_ID_DEVELOPMENT` | the `agentRuntimeId` of the already-created `fde-development-agent` runtime |

Find them once the runtimes exist (via the CDK stack's `CfnRuntime`
resources, or via the initial non-CDK
`fde-agents-deploy runtimes --agents engagement workflow development ...`
provisioning path — either way, `agentRuntimeId` is not the same string as
`agentRuntimeArn`, it is the ARN's trailing resource-id segment):

```bash
aws bedrock-agentcore-control list-agent-runtimes --region us-east-1 \
  --query "agentRuntimes[].{name:agentRuntimeName,id:agentRuntimeId}"
```

Then set them:

```bash
gh variable set FDE_RUNTIME_ID_ENGAGEMENT  --body "<id for fde-engagement-agent>"
gh variable set FDE_RUNTIME_ID_WORKFLOW    --body "<id for fde-workflow-agent>"
gh variable set FDE_RUNTIME_ID_DEVELOPMENT --body "<id for fde-development-agent>"
```

Until these three variables are set, `ci.yml`'s three "update ... agent
runtime" steps still run (gated only on `steps.creds.outputs.have`, the
existing AWS-secrets check) but pass an empty `--update` value —
`runtimes.py`'s `if args.update:` treats an empty string as falsy and
falls through to the create path instead, which either collides with the
already-existing runtime name (safe failure) or, if no runtime by that
name exists yet, creates one. Set all three before merging to `main` with
`AWS_DEPLOY_ROLE_ARN` configured.

## 6. First release

Once steps 1-4 are done:

```bash
git tag v0.1.0
git push origin v0.1.0
```

watch the `Release` workflow run, then open the printed launch URL from
its job summary (`https://console.aws.amazon.com/cloudformation/home?region=us-east-1#/stacks/create/review?templateURL=...`).
