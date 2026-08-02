"""Configuration: defaults, overrides, and that the cache can be cleared."""

from __future__ import annotations

import pytest

from fde_gate.config import GateSettings, get_gate_settings
from fde_mcp.config import get_settings


def test_defaults_and_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "FDE_GATE_DB_ROLE",
        "FDE_PRODOPS_DB_ROLE",
        "FDE_GATE_FUNCTION_NAME",
        "FDE_RUNTIME_ARN_ENGAGEMENT",
        "FDE_RUNTIME_ARN_WORKFLOW",
        "FDE_RUNTIME_ARN_DEVELOPMENT",
        "FDE_MCP_URL",
        "FDE_MCP_TOKEN",
        "FDE_GATE_MAX_STEP_ATTEMPTS",
        "FDE_GATE_STEP_TIMEOUT_SECONDS",
    ):
        monkeypatch.delenv(name, raising=False)

    defaults = GateSettings.from_env()
    assert defaults.gate_role == "fde_gate_service"
    assert defaults.prodops_role == "fde_prodops"
    assert defaults.max_step_attempts == 3
    # Unset means "run the step inline", never "silently skip it".
    assert defaults.self_function_name is None
    assert defaults.runtime_arn_for("workflow") is None
    assert defaults.runtime_arn_for("nonsense") is None

    monkeypatch.setenv("FDE_GATE_DB_ROLE", "custom_gate")
    monkeypatch.setenv("FDE_RUNTIME_ARN_WORKFLOW", "arn:aws:bedrock-agentcore:::runtime/fde-wf")
    monkeypatch.setenv("FDE_GATE_MAX_STEP_ATTEMPTS", "7")

    overridden = GateSettings.from_env()
    assert overridden.gate_role == "custom_gate"
    assert overridden.runtime_arn_for("workflow") == "arn:aws:bedrock-agentcore:::runtime/fde-wf"
    # Still None for the two personas that were not configured -- an unset
    # ARN must not fall back to a configured sibling.
    assert overridden.runtime_arn_for("engagement") is None
    assert overridden.max_step_attempts == 7


def test_cache_clear_re_reads_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """`get_gate_settings` caches, and both caches must be cleared to re-read.

    `Settings.db` comes from `fde_mcp.config`, so clearing only this module's
    cache leaves the database settings stale -- which is exactly the trap a
    test that monkeypatches `FDE_DB_*` would fall into.
    """
    monkeypatch.setenv("FDE_GATE_DB_ROLE", "first_role")
    get_gate_settings.cache_clear()
    get_settings.cache_clear()
    assert get_gate_settings().gate.gate_role == "first_role"

    monkeypatch.setenv("FDE_GATE_DB_ROLE", "second_role")
    assert get_gate_settings().gate.gate_role == "first_role", "must be cached until cleared"

    get_gate_settings.cache_clear()
    get_settings.cache_clear()
    assert get_gate_settings().gate.gate_role == "second_role"
