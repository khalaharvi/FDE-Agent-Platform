"""providers.py -- the model-backend registry: one factory, five backends.

`build_model` is the only place a strands model class is constructed for
agent turns (runtime.py calls it where `BedrockModel(...)` used to be
hardcoded). Provider SDK imports are function-local so `bedrock` deployments
never import anthropic/openai/google-genai.
"""

from __future__ import annotations

from typing import Any

from fde_agents.common.config import ModelBackendSettings
from fde_mcp.credentials import CredentialError

PROVIDER_KEYS = frozenset({"bedrock", "anthropic", "openai", "gemini", "openai-compat"})

COMPAT_PRESETS: dict[str, str] = {
    "opencode-zen": "https://opencode.ai/zen/v1",
    "ollama": "http://127.0.0.1:11434/v1",
    "lmstudio": "http://127.0.0.1:1234/v1",
}


def _resolve_api_key(provider: str) -> str:
    from fde_mcp.credentials import resolve_api_key  # noqa: PLC0415

    return resolve_api_key(provider)


def _bedrock_cls() -> Any:
    from strands.models.bedrock import BedrockModel  # noqa: PLC0415

    return BedrockModel


def _anthropic_cls() -> Any:
    from strands.models.anthropic import AnthropicModel  # noqa: PLC0415

    return AnthropicModel


def _openai_cls() -> Any:
    from strands.models.openai import OpenAIModel  # noqa: PLC0415

    return OpenAIModel


def _gemini_cls() -> Any:
    from strands.models.gemini import GeminiModel  # noqa: PLC0415

    return GeminiModel


def resolve_base_url(backend: ModelBackendSettings) -> str:
    if backend.base_url:
        return backend.base_url
    if backend.compat_preset:
        url = COMPAT_PRESETS.get(backend.compat_preset)
        if url is None:
            msg = (
                f"FDE_MODEL_COMPAT_PRESET={backend.compat_preset!r} is not a preset; "
                f"choose one of {sorted(COMPAT_PRESETS)} or set FDE_MODEL_BASE_URL"
            )
            raise ValueError(msg)
        return url
    msg = "openai-compat provider requires FDE_MODEL_BASE_URL or FDE_MODEL_COMPAT_PRESET"
    raise ValueError(msg)


def qualified_model_id(provider: str, model_id: str) -> str:
    """What the audit trail records. Bare id for bedrock (keeps existing
    trace rows comparable); "provider/model" otherwise, so the authoring
    model is unambiguous across providers.
    """
    return model_id if provider == "bedrock" else f"{provider}/{model_id}"


def build_model(model_id: str, backend: ModelBackendSettings) -> Any:
    provider = backend.provider
    if provider == "bedrock":
        return _bedrock_cls()(model_id=model_id)
    if provider == "anthropic":
        return _anthropic_cls()(
            client_args={"api_key": _resolve_api_key(provider)},
            model_id=model_id,
            max_tokens=backend.max_tokens,
        )
    if provider == "openai":
        return _openai_cls()(client_args={"api_key": _resolve_api_key(provider)}, model_id=model_id)
    if provider == "gemini":
        return _gemini_cls()(client_args={"api_key": _resolve_api_key(provider)}, model_id=model_id)
    if provider == "openai-compat":
        try:
            key = _resolve_api_key(provider)
        except CredentialError:  # keyless local servers are fine
            key = "local"
        return _openai_cls()(
            client_args={
                "api_key": key,
                "base_url": resolve_base_url(backend),
            },
            model_id=model_id,
        )
    msg = f"FDE_MODEL_PROVIDER={provider!r} invalid; choose one of {sorted(PROVIDER_KEYS)}"
    raise ValueError(msg)
