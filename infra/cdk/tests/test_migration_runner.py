from __future__ import annotations

import sys
from pathlib import Path

import pytest

# `handler.py` lives in `lambdas/migration_runner/`, not on the normal
# `fde_cdk`/`tests` import path -- see its own module docstring for why it
# is importable here at all without `psycopg`/`boto3` installed in this
# venv (both are imported lazily, inside the functions that need them).
sys.path.insert(0, str(Path(__file__).parents[1] / "lambdas" / "migration_runner"))
from handler import plan_migrations

from tests.test_synth import synth_template


def test_plan_applies_only_pending_in_order() -> None:
    available = ["001_a.sql", "002_b.sql", "003_c.sql"]
    assert plan_migrations({"001_a.sql"}, available) == ["002_b.sql", "003_c.sql"]


def test_plan_refuses_gap() -> None:
    with pytest.raises(ValueError, match=r"002_b\.sql"):
        plan_migrations({"001_a.sql", "003_c.sql"}, ["001_a.sql", "002_b.sql", "003_c.sql"])


def test_plan_noop_when_current() -> None:
    assert plan_migrations({"001_a.sql"}, ["001_a.sql"]) == []


def test_plan_empty_ledger_applies_everything_in_order() -> None:
    available = ["003_c.sql", "001_a.sql", "002_b.sql"]  # deliberately out of order
    assert plan_migrations(set(), available) == ["001_a.sql", "002_b.sql", "003_c.sql"]


def test_plan_refuses_gap_even_when_only_the_gap_is_missing() -> None:
    # Everything but the middle one is applied -- still a gap, not "one
    # pending migration".
    with pytest.raises(ValueError, match=r"002_b\.sql"):
        plan_migrations(
            {"001_a.sql", "003_c.sql", "004_d.sql"},
            ["001_a.sql", "002_b.sql", "003_c.sql", "004_d.sql"],
        )


def test_plan_ignores_applied_entries_not_in_available() -> None:
    # A ledger row for a migration file that no longer ships (e.g. an old
    # release) must not be treated as a gap -- only `available` entries
    # are walked.
    assert plan_migrations({"000_retired.sql", "001_a.sql"}, ["001_a.sql", "002_b.sql"]) == [
        "002_b.sql"
    ]


# ---------------------------------------------------------------------
# Synth: the custom resource, its Lambda's code location, and VPC
# attachment.
# ---------------------------------------------------------------------


def test_custom_resource_exists_with_release_tag_and_admin_email() -> None:
    t = synth_template()
    resources = t.find_resources("AWS::CloudFormation::CustomResource")
    assert len(resources) == 1
    resource = next(iter(resources.values()))
    assert resource["Properties"]["ReleaseTag"] == {"Ref": "ReleaseTag"}
    assert resource["Properties"]["AdminEmail"] == {"Ref": "AdminEmail"}
    assert "ServiceToken" in resource["Properties"]


def test_custom_resource_service_token_points_at_the_migration_function() -> None:
    t = synth_template()
    template = t.to_json()
    functions = {
        k: v for k, v in template["Resources"].items() if v["Type"] == "AWS::Lambda::Function"
    }
    assert len(functions) == 1
    function_logical_id = next(iter(functions))

    resource = next(iter(t.find_resources("AWS::CloudFormation::CustomResource").values()))
    assert resource["Properties"]["ServiceToken"] == {"Fn::GetAtt": [function_logical_id, "Arn"]}


def test_migration_function_is_python312_arm64_and_vpc_attached() -> None:
    t = synth_template()
    functions = t.find_resources("AWS::Lambda::Function")
    assert len(functions) == 1
    fn = next(iter(functions.values()))
    props = fn["Properties"]
    assert props["Runtime"] == "python3.12"
    assert props["Architectures"] == ["arm64"]
    assert props["Handler"] == "handler.handler"
    assert props["Timeout"] == 900
    assert "VpcConfig" in props
    assert len(props["VpcConfig"]["SubnetIds"]) == 2


def test_migration_function_code_points_at_release_tag_key() -> None:
    t = synth_template()
    fn = next(iter(t.find_resources("AWS::Lambda::Function").values()))
    code = fn["Properties"]["Code"]
    assert code["S3Key"] == {
        "Fn::Join": ["", ["releases/", {"Ref": "ReleaseTag"}, "/migration-runner.zip"]]
    }


def test_migration_function_code_bucket_is_fn_if_on_has_assets_bucket() -> None:
    """The Task 2 -> Task 5 deferral this task closes: `AssetsBucket`
    (blank by default) resolves via `HasAssetsBucket` to either the
    override param or `AssetsRegionMap`'s region default."""
    t = synth_template()
    fn = next(iter(t.find_resources("AWS::Lambda::Function").values()))
    bucket = fn["Properties"]["Code"]["S3Bucket"]
    assert bucket == {
        "Fn::If": [
            "HasAssetsBucket",
            {"Ref": "AssetsBucket"},
            {"Fn::FindInMap": ["AssetsRegionMap", {"Ref": "AWS::Region"}, "bucket"]},
        ]
    }


def test_migration_function_uses_the_migration_role() -> None:
    t = synth_template()
    template = t.to_json()
    roles = {k: v for k, v in template["Resources"].items() if v["Type"] == "AWS::IAM::Role"}
    migration_role_id = next(
        k
        for k, v in roles.items()
        for policy in v["Properties"].get("Policies", [])
        if policy["PolicyName"] == "migration-permissions"
    )
    fn = next(iter(t.find_resources("AWS::Lambda::Function").values()))
    assert fn["Properties"]["Role"] == {"Fn::GetAtt": [migration_role_id, "Arn"]}


def test_migration_function_has_db_secret_arn_env_var() -> None:
    t = synth_template()
    fn = next(iter(t.find_resources("AWS::Lambda::Function").values()))
    env = fn["Properties"]["Environment"]["Variables"]
    assert "DB_SECRET_ARN" in env


def test_no_provider_framework_lambda_in_template() -> None:
    """Guards the Provider-free contract from this construct's own side:
    `aws_cdk.custom_resources.Provider` would add a SECOND
    `AWS::Lambda::Function` (its onEvent handler) plus a log-retention
    custom resource of its own. Only the migration-runner function itself
    should exist."""
    t = synth_template()
    assert len(t.find_resources("AWS::Lambda::Function")) == 1
    assert len(t.find_resources("AWS::CloudFormation::CustomResource")) == 1


def test_has_assets_bucket_condition_present_and_used() -> None:
    template = synth_template().to_json()
    assert "HasAssetsBucket" in template["Conditions"]
