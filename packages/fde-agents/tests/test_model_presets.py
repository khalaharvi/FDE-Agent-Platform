"""The budget dial: FDE_MODEL_ID > FDE_MODEL_PRESET[agent] > DEFAULT_MODEL_ID.

A wrong resolution here silently selects a differently-priced model, so
every arm of the order is pinned, including the loud failures. The deploy
CLI must resolve identically (it bakes the answer into each runtime's env),
so both resolvers are tested against the same expectations.
"""

from __future__ import annotations

import argparse

import pytest

from fde_agents.common.config import DEFAULT_MODEL_ID, MODEL_PRESETS, resolve_model_id
from fde_agents.deploy.runtimes import _resolved_model_id


def test_default_when_nothing_set(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("FDE_MODEL_ID", raising=False)
    monkeypatch.delenv("FDE_MODEL_PRESET", raising=False)
    assert resolve_model_id("engagement") == DEFAULT_MODEL_ID


def test_preset_resolves_per_agent(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("FDE_MODEL_ID", raising=False)
    monkeypatch.setenv("FDE_MODEL_PRESET", "balanced")
    assert resolve_model_id("engagement") == MODEL_PRESETS["balanced"]["engagement"]
    assert resolve_model_id("development") == DEFAULT_MODEL_ID  # balanced keeps Claude on codegen


def test_explicit_model_id_beats_preset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FDE_MODEL_ID", "us.example.model-v1:0")
    monkeypatch.setenv("FDE_MODEL_PRESET", "budget")
    assert resolve_model_id("workflow") == "us.example.model-v1:0"


def test_unknown_preset_fails_loudly(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("FDE_MODEL_ID", raising=False)
    monkeypatch.setenv("FDE_MODEL_PRESET", "bugdet")  # the typo this guard exists for
    with pytest.raises(ValueError, match="not a preset"):
        resolve_model_id("engagement")


def test_unknown_agent_in_preset_fails_loudly(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("FDE_MODEL_ID", raising=False)
    monkeypatch.setenv("FDE_MODEL_PRESET", "budget")
    with pytest.raises(ValueError, match="no entry for agent"):
        resolve_model_id("mystery")


def test_every_preset_covers_every_agent() -> None:
    for name, preset in MODEL_PRESETS.items():
        assert set(preset) == {"engagement", "workflow", "development"}, name


def _deploy_args(model_id: str | None, preset: str | None) -> argparse.Namespace:
    return argparse.Namespace(model_id=model_id, model_preset=preset)


def test_deploy_resolution_matches_runtime_order() -> None:
    assert _resolved_model_id("engagement", _deploy_args(None, None)) == DEFAULT_MODEL_ID
    assert (
        _resolved_model_id("engagement", _deploy_args(None, "balanced"))
        == MODEL_PRESETS["balanced"]["engagement"]
    )
    assert _resolved_model_id("development", _deploy_args(None, "balanced")) == DEFAULT_MODEL_ID
    assert _resolved_model_id("workflow", _deploy_args("us.x.y-v1:0", "budget")) == "us.x.y-v1:0"
