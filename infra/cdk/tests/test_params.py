from __future__ import annotations

import pytest

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


def test_conditions_and_mapping_present() -> None:
    """The rest of the Interfaces contract: the three named conditions and
    the region->assets-bucket mapping, both consumed by later tasks."""
    template = synth_template().to_json()
    assert set(template["Conditions"]) == {"IsProduction", "IsBedrockModel", "HasProviderKey"}
    assert template["Mappings"]["AssetsRegionMap"]["us-east-1"]["bucket"]


@pytest.mark.xfail(
    reason="flips in Task 3 with first real resource (network/database constructs)",
    strict=False,
)
def test_no_cdk_metadata_resource() -> None:
    """Forcing function for the analytics_reporting=True workaround (see
    app.py / README): this task adds Parameters/Conditions/Mappings only,
    no Resources, so cfn-lint's E1001 ('Resources' is a required property)
    still needs the synthetic CDKMetadata resource to keep the template
    non-empty. Un-xfail this the moment a later task's first real construct
    lands and app.py flips analytics_reporting to False.

    Caveat, spelled out rather than left implicit: `synth_template()` builds
    its own bare `cdk.App()` (see tests/test_synth.py), which never sets
    `analytics_reporting` at all -- so this assertion is currently vacuously
    true (there are no Resources of ANY kind via this helper, CDKMetadata
    included) rather than a genuine red/green signal against app.py's real
    config. See `test_cdk_metadata_present_via_real_app_config` below for
    the test that actually exercises app.py's `analytics_reporting=True`."""
    resource_types = {r["Type"] for r in synth_template().to_json().get("Resources", {}).values()}
    assert "AWS::CDK::Metadata" not in resource_types


def test_cdk_metadata_present_via_real_app_config() -> None:
    """Companion to the xfail test above, using the SAME App() construction
    app.py actually uses (`analytics_reporting=True`), unlike
    `synth_template()`'s bare `cdk.App()`. This is what really keeps
    cfn-lint's E1001 satisfied today, given this task adds no Resources of
    its own. Task 3 should delete or invert this test in the same commit
    that flips `analytics_reporting` to False in app.py."""
    import aws_cdk as cdk
    from aws_cdk.assertions import Template

    from fde_cdk.stack import FdePlatformStack

    app = cdk.App(analytics_reporting=True)
    stack = FdePlatformStack(app, "FdePlatform")
    resource_types = {
        r["Type"] for r in Template.from_stack(stack).to_json().get("Resources", {}).values()
    }
    assert "AWS::CDK::Metadata" in resource_types
