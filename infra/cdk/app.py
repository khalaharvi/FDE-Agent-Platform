#!/usr/bin/env python3
"""CDK app entry point -- invoked as `uv run python app.py` (see cdk.json).

Builds the single root stack behind the README Launch-Stack button and
synthesizes it. No CDK CLI / Node.js required: `cdk.json`'s "app" command
runs this script directly, and cfn-lint validates the emitted template.
"""

from __future__ import annotations

import aws_cdk as cdk

from fde_cdk.stack import FdePlatformStack

# Explicit outdir: running `python app.py` directly (no Node `cdk` CLI, see
# README) means nothing else sets CDK_OUTDIR, and the default falls back to
# a throwaway system temp directory -- cfn-lint (and CI) need a stable,
# repo-relative path to point at.
#
# analytics_reporting=False (default): through Task 2 this was True as a
# workaround -- the Node `cdk` CLI turns it on by default, which is where
# the (harmless, asset-free) `AWS::CDK::Metadata` resource normally comes
# from, and bypassing that CLI entirely meant nothing else would give the
# template a non-empty `Resources` section (CloudFormation's own schema
# fails an empty one; cfn-lint catches that as E1001) while Task 2 added
# only Parameters/Conditions/Mappings. Task 3's Network/Database constructs
# give the stack genuine Resources, so the workaround is retired here --
# see README's (former) "currently-expected quirk" section and
# tests/test_params.py's test_no_cdk_metadata_resource /
# test_no_cdk_metadata_via_real_app_config for the forcing function this
# resolves.
app = cdk.App(outdir="cdk.out")
stack = FdePlatformStack(app, "FdePlatform")

# Task 7.5 hygiene: cost-allocation tags on every taggable resource in the
# stack, unconditional (not gated by OpsMode -- tagging costs nothing and
# is useful even with the ops layer off). `cdk.Tags` is an Aspect, applied
# during `app.synth()` below, so it does not matter that `stack` already
# exists by the time these two calls run.
#
# "stack-tier" is bound to the SAME `DeployTier` launch parameter
# `database.py`'s Fn::If tier-switching already reads (`stack.params` is
# public on `FdePlatformStack`) rather than a fixed literal: this repo
# already treats demo/production as the one axis a cost report would want
# to split by, and a `CfnParameter` token is a valid CloudFormation Tag
# value (verified via a standalone synth) the same way it is a valid
# environment-variable value elsewhere in this stack.
cdk.Tags.of(app).add("app", "fde-platform")
cdk.Tags.of(app).add("stack-tier", stack.params.deploy_tier.value_as_string)

app.synth()
