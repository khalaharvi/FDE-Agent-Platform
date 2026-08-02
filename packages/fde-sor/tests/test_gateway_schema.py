"""The contract between the Gateway tool schema and the Lambda behind it.

This test exists because the two had already drifted. `gateway.py` used to
build its `lambda` target's tool schema from a placeholder dict written inline,
describing a tool that "returns immediately with a job id" -- which
`backfill_handler` does not do and never did. An agent reading that schema over
MCP would have called it expecting a job id and got a stats object. Nothing
connected the schema to the handler, so nothing caught it.

Now the schema is a packaged JSON file and this test pins it to
`backfill.BACKFILL_PARAMS`, the same tuple `backfill_handler` validates
incoming arguments against. The two cannot diverge without a red test.

Placement note for the integrator: this belongs next to `gateway.py` in
`packages/fde-agents/tests/`. It lives here because this slice's file
ownership covers `fde-agents`' deploy sources but not its test directory, and
`fde_agents` is installed in the same workspace virtualenv either way. Moving
it needs no edit beyond the path.
"""

from __future__ import annotations

from typing import Any

import pytest

from fde_sor.backfill import BACKFILL_PARAMS, BACKFILL_REQUIRED_PARAMS
from fde_sor.lambda_handlers import backfill_handler

#: Never opened -- only fed to argparse, to prove the override flag survives.
CUSTOM_SCHEMA_PATH = "./custom-tool-schema.json"

gateway = pytest.importorskip(
    "fde_agents.deploy.gateway",
    reason="fde-agents is not installed in this environment (run `uv sync --all-packages`)",
)


@pytest.fixture(scope="module")
def tool() -> dict[str, Any]:
    schema = gateway.load_default_tool_schema()
    assert isinstance(schema, list), "the Gateway expects a LIST of tool definitions"
    assert len(schema) == 1
    return dict(schema[0])


def test_the_packaged_schema_loads_from_package_resources(tool: dict[str, Any]) -> None:
    """`importlib.resources`, not a path relative to `__file__`: the schema has
    to resolve from an installed wheel and from `deploy/codezip.py`'s zipped
    code artifact, not only from a source checkout.
    """
    assert tool["name"] == "sor_backfill_observations"
    assert tool["description"].strip()
    assert tool["inputSchema"]["type"] == "object"


def test_schema_properties_match_the_handler_exactly(tool: dict[str, Any]) -> None:
    assert set(tool["inputSchema"]["properties"]) == set(BACKFILL_PARAMS)


def test_schema_required_params_match_the_handler_exactly(tool: dict[str, Any]) -> None:
    assert set(tool["inputSchema"]["required"]) == set(BACKFILL_REQUIRED_PARAMS)


def test_every_property_is_documented(tool: dict[str, Any]) -> None:
    """The description is what an agent reads to decide whether and how to call
    this. An undocumented parameter is one it will guess at.
    """
    for name, spec in tool["inputSchema"]["properties"].items():
        assert spec.get("description", "").strip(), f"{name} has no description"
        assert spec.get("type"), f"{name} has no type"


def test_the_description_does_not_promise_a_fire_and_forget_call(tool: dict[str, Any]) -> None:
    """The exact drift this test exists to stop recurring.

    The old placeholder claimed the tool "returns immediately with a job id".
    `backfill_handler` blocks for the duration of the replay and returns the
    ingest stats, so an agent that believed the schema would have waited for a
    job id that never arrives.
    """
    description = tool["description"].lower()
    assert "returns immediately" not in description
    assert "idempotent" in description, "the caller needs to know a retry is safe"


def test_the_handler_rejects_arguments_the_schema_does_not_declare() -> None:
    """Agreement in both directions: the schema is not merely a superset of
    what the handler accepts.
    """
    result = backfill_handler(
        {
            "engagement_id": "8b1d3f2a-0000-4000-8000-000000000001",
            "adapter_key": "jira-prod",
            "s3_uri": "s3://bucket/key.jsonl",
            "not_in_the_schema": True,
        }
    )
    assert "error" in result
    assert "not_in_the_schema" in result["error"]


@pytest.mark.parametrize("missing", BACKFILL_REQUIRED_PARAMS)
def test_the_handler_reports_each_missing_required_parameter(missing: str) -> None:
    args = {
        "engagement_id": "8b1d3f2a-0000-4000-8000-000000000001",
        "adapter_key": "jira-prod",
        "s3_uri": "s3://bucket/key.jsonl",
    }
    del args[missing]
    result = backfill_handler(args)
    assert "error" in result
    assert missing in result["error"]


def test_the_override_flag_still_exists() -> None:
    """`--lambda-tool-schema-file` remains, for a deployment that registers
    additional Lambda-backed tools; the packaged schema is the DEFAULT, not a
    hard-coding.
    """
    parser_args = gateway._parse_args(
        [
            "--role-arn",
            "arn:aws:iam::123456789012:role/gw",
            "--jwt-discovery-url",
            "https://example.invalid/.well-known/openid-configuration",
            "--jwt-allowed-audience",
            "aud",
            "--mcp-server-endpoint",
            "https://example.invalid/mcp",
            "--lambda-tool-schema-file",
            CUSTOM_SCHEMA_PATH,
        ]
    )
    assert parser_args.lambda_tool_schema_file == CUSTOM_SCHEMA_PATH
