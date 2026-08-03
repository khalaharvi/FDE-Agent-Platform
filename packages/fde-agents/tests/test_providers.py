"""Unit tests for the provider registry. Strands model classes are
monkeypatched via the per-provider `_*_cls` loader seams, so no provider
SDK import (and no network) happens here.
"""

from __future__ import annotations

from typing import Any

import pytest

from fde_agents.common import providers
from fde_agents.common.config import ModelBackendSettings


class _Recorder:
    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs


def _backend(**overrides: Any) -> ModelBackendSettings:
    defaults: dict[str, Any] = {
        "provider": "bedrock",
        "base_url": None,
        "compat_preset": None,
        "max_tokens": 8192,
    }
    defaults.update(overrides)
    return ModelBackendSettings(**defaults)


@pytest.fixture(autouse=True)
def _no_real_sdks(monkeypatch: pytest.MonkeyPatch) -> None:
    for loader in ("_bedrock_cls", "_anthropic_cls", "_openai_cls", "_gemini_cls"):
        monkeypatch.setattr(providers, loader, lambda: _Recorder)
    monkeypatch.setattr(providers, "_resolve_api_key", lambda provider: f"key-{provider}")


def test_bedrock_default_builds_without_api_key() -> None:
    model = providers.build_model("anthropic.claude-sonnet-5", _backend())
    assert isinstance(model, _Recorder)
    assert model.kwargs == {"model_id": "anthropic.claude-sonnet-5"}


def test_anthropic_passes_key_and_max_tokens() -> None:
    model = providers.build_model("claude-sonnet-5", _backend(provider="anthropic"))
    assert model.kwargs["client_args"] == {"api_key": "key-anthropic"}
    assert model.kwargs["model_id"] == "claude-sonnet-5"
    assert model.kwargs["max_tokens"] == 8192


def test_openai_and_gemini_pass_key() -> None:
    m1 = providers.build_model("gpt-5.2", _backend(provider="openai"))
    assert m1.kwargs == {"client_args": {"api_key": "key-openai"}, "model_id": "gpt-5.2"}
    m2 = providers.build_model("gemini-3-pro", _backend(provider="gemini"))
    assert m2.kwargs == {"client_args": {"api_key": "key-gemini"}, "model_id": "gemini-3-pro"}


def test_compat_preset_resolves_and_explicit_base_url_wins() -> None:
    b = _backend(provider="openai-compat", compat_preset="opencode-zen")
    assert providers.resolve_base_url(b) == "https://opencode.ai/zen/v1"
    b2 = _backend(provider="openai-compat", compat_preset="opencode-zen", base_url="http://x:1/v1")
    assert providers.resolve_base_url(b2) == "http://x:1/v1"
    model = providers.build_model("qwen3-coder", b2)
    assert model.kwargs["client_args"] == {
        "api_key": "key-openai-compat",
        "base_url": "http://x:1/v1",
    }


def test_compat_without_url_or_preset_raises() -> None:
    with pytest.raises(ValueError, match="FDE_MODEL_BASE_URL"):
        providers.build_model("m", _backend(provider="openai-compat"))


def test_unknown_provider_and_unknown_preset_raise() -> None:
    with pytest.raises(ValueError, match="FDE_MODEL_PROVIDER"):
        providers.build_model("m", _backend(provider="grok-on-a-floppy"))
    with pytest.raises(ValueError, match="FDE_MODEL_COMPAT_PRESET"):
        providers.resolve_base_url(_backend(provider="openai-compat", compat_preset="nope"))


def test_qualified_model_id() -> None:
    assert providers.qualified_model_id("bedrock", "anthropic.claude-sonnet-5") == (
        "anthropic.claude-sonnet-5"
    )
    assert providers.qualified_model_id("anthropic", "claude-sonnet-5") == (
        "anthropic/claude-sonnet-5"
    )
