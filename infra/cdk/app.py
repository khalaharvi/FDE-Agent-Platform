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
# analytics_reporting=True: the Node `cdk` CLI turns this on by default,
# which is where the (harmless, asset-free) `AWS::CDK::Metadata` resource
# normally comes from. We bypass that CLI entirely, so it defaults to off --
# set it explicitly so the template has a non-empty `Resources` section
# (a template with no Resources at all fails CloudFormation's own schema,
# and cfn-lint catches that as E1001). Task 2 added Parameters/Conditions/
# Mappings but deliberately no Resources yet (nothing consumes them until
# task 3+), so this is STILL load-bearing after task 2 -- see README's
# "currently-expected quirk" section and tests/test_params.py's
# test_no_cdk_metadata_resource / test_cdk_metadata_present_via_real_app_config
# for the forcing function that flips this to False.
app = cdk.App(outdir="cdk.out", analytics_reporting=True)
FdePlatformStack(app, "FdePlatform")
app.synth()
