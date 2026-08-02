"""Tests for `fde_training.config` plus the same-repo schema-constants
consistency check that used to live at the bottom of the pre-split
`training/test_grpo_rewards.py`.

No live Postgres or AWS needed. Every test that touches settings clears
both `fde_training.config.get_settings` and `fde_mcp.config.get_settings`'s
caches before and after -- `fde_training.config.Settings.db` is sourced
FROM `fde_mcp.config`, so a monkeypatched environment is only actually
observed once both caches are cleared (see `fde_training.config`'s module
docstring on why database settings are reused rather than redeclared).
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from fde_mcp.config import get_settings as get_mcp_settings
from fde_mcp.tools._base import EDGE_TYPES as MCP_EDGE_TYPES
from fde_mcp.tools._base import NODE_TYPES as MCP_NODE_TYPES
from fde_training.config import Settings, TrainingRoleSettings, get_settings
from fde_training.rewards import EDGE_TYPES, NODE_TYPES


@pytest.fixture(autouse=True)
def _clear_settings_cache() -> Iterator[None]:
    get_settings.cache_clear()
    get_mcp_settings.cache_clear()
    yield
    get_settings.cache_clear()
    get_mcp_settings.cache_clear()


def test_get_settings_is_cached_across_calls() -> None:
    first = get_settings()
    second = get_settings()
    assert first is second


def test_get_settings_reflects_env_after_cache_clear(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FDE_DB_NAME", "not_the_default")
    get_settings.cache_clear()
    get_mcp_settings.cache_clear()
    assert get_settings().db.name == "not_the_default"


def test_settings_db_is_reused_from_fde_mcp_not_redeclared() -> None:
    """`fde_training.config.Settings.db` must be the SAME
    `fde_mcp.config.DatabaseSettings` instance `fde_mcp` itself hands out --
    that identity is what "reused, not redeclared" means in practice."""
    assert get_settings().db is get_mcp_settings().db


def test_training_role_settings_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("FDE_DB_TRAINING_ROLE", raising=False)
    monkeypatch.delenv("FDE_DB_ROLLOUT_ROLE", raising=False)
    roles = TrainingRoleSettings.from_env()
    assert roles.training_role == "fde_training"
    assert roles.rollout_role == "fde_rl_rollout"


def test_training_role_settings_overridable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FDE_DB_TRAINING_ROLE", "custom_training_role")
    monkeypatch.setenv("FDE_DB_ROLLOUT_ROLE", "custom_rollout_role")
    roles = TrainingRoleSettings.from_env()
    assert roles.training_role == "custom_training_role"
    assert roles.rollout_role == "custom_rollout_role"


def test_settings_is_frozen() -> None:
    settings = get_settings()
    with pytest.raises(AttributeError):
        settings.roles = TrainingRoleSettings.from_env()  # type: ignore[misc]


def test_settings_from_env_assembles_db_and_roles() -> None:
    settings = Settings.from_env()
    assert settings.db.name  # sourced from fde_mcp.config, always has a value
    assert settings.roles.training_role
    assert settings.roles.rollout_role


# ===========================================================================
# Consistency with fde_mcp.tools._base -- catches the reward package's
# deliberately-duplicated schema vocabulary drifting apart from the MCP
# server's copy (see fde_training.rewards.validity's module docstring for
# why the duplication exists in the first place).
# ===========================================================================
def test_schema_constants_match_fde_mcp() -> None:
    assert set(MCP_NODE_TYPES) == NODE_TYPES
    assert set(MCP_EDGE_TYPES) == EDGE_TYPES
