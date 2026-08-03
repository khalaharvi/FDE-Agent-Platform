from __future__ import annotations

from tests.test_synth import synth_template


def test_vpc_two_azs_one_nat() -> None:
    t = synth_template()
    t.resource_count_is("AWS::EC2::NatGateway", 1)
    assert (
        len(
            [
                k
                for k in t.to_json()["Resources"]
                if "PublicSubnet" in k and t.to_json()["Resources"][k]["Type"] == "AWS::EC2::Subnet"
            ]
        )
        == 2
    )


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
    # provider key secret comes later; adjust in Task 6 if needed
    t.resource_count_is("AWS::SecretsManager::Secret", 1)
