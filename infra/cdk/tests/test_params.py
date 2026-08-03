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


def test_conditions_and_mapping_present() -> None:
    """The rest of the Interfaces contract: the three named conditions and
    the region->assets-bucket mapping, both consumed by later tasks."""
    template = synth_template().to_json()
    assert set(template["Conditions"]) == {"IsProduction", "IsBedrockModel", "HasProviderKey"}
    assert template["Mappings"]["AssetsRegionMap"]["us-east-1"]["bucket"]


def test_no_cdk_metadata_resource() -> None:
    """Forcing function for the analytics_reporting=True workaround (see
    app.py / README), now resolved: Task 2 had no Resources of its own, so
    cfn-lint's E1001 ('Resources' is a required property) needed the
    synthetic CDKMetadata resource to keep the template non-empty. Task 3's
    Network/Database constructs give the stack genuine Resources, so
    app.py's analytics_reporting flips to False in this same commit and
    this test drops the `xfail(strict=False)` it carried through Task 2.

    `synth_template()` builds its own bare `cdk.App()` (see
    tests/test_synth.py), which never set `analytics_reporting` at all --
    so this assertion was already true before Task 3 (vacuously, since
    there were no Resources of any kind via this helper) and stays true now
    for a real reason (Network/Database resources exist, and still no
    CDKMetadata). `test_no_cdk_metadata_via_real_app_config` below is the
    companion that exercises app.py's actual (now False) configuration."""
    resource_types = {r["Type"] for r in synth_template().to_json().get("Resources", {}).values()}
    assert "AWS::CDK::Metadata" not in resource_types


def test_no_cdk_metadata_via_real_app_config() -> None:
    """Companion to the test above, using the SAME App() construction
    app.py actually uses (`analytics_reporting=False`, post-Task-3), unlike
    `synth_template()`'s bare `cdk.App()`. Inverted from its Task 2
    predecessor (`test_cdk_metadata_present_via_real_app_config`, which
    asserted CDKMetadata WAS present under the old `analytics_reporting=
    True` workaround): now that the stack has genuine Resources
    (Network/Database), app.py no longer needs the workaround, and this
    proves the real app.py invocation stays E1001-clean without it."""
    import aws_cdk as cdk
    from aws_cdk.assertions import Template

    from fde_cdk.stack import FdePlatformStack

    app = cdk.App(analytics_reporting=False)
    stack = FdePlatformStack(app, "FdePlatform")
    resource_types = {
        r["Type"] for r in Template.from_stack(stack).to_json().get("Resources", {}).values()
    }
    assert "AWS::CDK::Metadata" not in resource_types
    assert resource_types  # E1001 guard: Resources section must be non-empty
