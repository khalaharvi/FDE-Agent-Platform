"""credentials.py -- API-key resolution for non-Bedrock model providers.

Resolution order, per provider: environment variable > OS keychain > error.
Servers and deployed runtimes therefore never depend on a keychain: they set
the env var (sub-project 2 maps Secrets Manager -> env), while a developer
laptop uses `fde-providers login <provider>` once and forgets about it.

Bedrock is deliberately absent from this module: it authenticates through
the ambient AWS credential chain, not an API key.
"""

from __future__ import annotations

import os

from fde_mcp.config import get_settings

KEYCHAIN_SERVICE = "fde-platform"

# provider key -> primary env var. `gemini` also honours GOOGLE_API_KEY
# (the google-genai SDK's own convention) as a fallback, below.
PROVIDER_ENV_VARS: dict[str, str] = {
    "anthropic": "ANTHROPIC_API_KEY",
    "openai": "OPENAI_API_KEY",
    "gemini": "GEMINI_API_KEY",
    "openai-compat": "FDE_MODEL_API_KEY",
}

_FALLBACK_ENV_VARS: dict[str, str] = {"gemini": "GOOGLE_API_KEY"}

# Named openai-compat endpoints, keyed by `FDE_MODEL_COMPAT_PRESET`. Lives
# here (rather than in fde_agents.common.providers, its original home)
# because fde_mcp.providers_cli's login-time validation needs it too, and
# fde-mcp must not import fde-agents; fde_agents.common.providers re-exports
# this dict under the same public name so existing callers are unaffected.
COMPAT_PRESETS: dict[str, str] = {
    "opencode-zen": "https://opencode.ai/zen/v1",
    "ollama": "http://127.0.0.1:11434/v1",
    "lmstudio": "http://127.0.0.1:1234/v1",
}


class CredentialError(RuntimeError):
    """A provider API key is missing or the provider is unknown."""


def _keyring_get(account: str) -> str | None:
    import keyring  # noqa: PLC0415 -- keep keyring optional until first use
    import keyring.errors  # noqa: PLC0415

    try:
        return keyring.get_password(KEYCHAIN_SERVICE, account)
    except keyring.errors.KeyringError:
        # A locked/absent backend means "no stored key", not a crash --
        # the caller falls through to the actionable CredentialError.
        return None


def _keyring_set(account: str, key: str) -> None:
    import keyring  # noqa: PLC0415

    keyring.set_password(KEYCHAIN_SERVICE, account, key)


def _keyring_delete(account: str) -> bool:
    import keyring  # noqa: PLC0415
    import keyring.errors  # noqa: PLC0415

    try:
        keyring.delete_password(KEYCHAIN_SERVICE, account)
    except keyring.errors.KeyringError:
        # A locked/absent backend or missing key means "delete failed", not a crash --
        # the caller falls through to returning False, same contract as _keyring_get.
        return False
    return True


def _require_known(provider: str) -> str:
    env_var = PROVIDER_ENV_VARS.get(provider)
    if env_var is None:
        msg = f"unknown provider {provider!r}; expected one of {sorted(PROVIDER_ENV_VARS)}"
        raise CredentialError(msg)
    return env_var


def peek_api_key(provider: str) -> tuple[str | None, str]:
    """The key for `provider` and where it came from, without raising on
    absence: ("env"|"keychain"|"none"). `fde-providers status` uses this.
    """
    env_var = _require_known(provider)
    # For openai-compat, read from settings; for vendor vars, read from os.environ.
    if provider == "openai-compat":
        from_env = get_settings().agent.model_api_key
    else:
        from_env = os.environ.get(env_var)
        if not from_env:
            fallback = _FALLBACK_ENV_VARS.get(provider)
            if fallback:
                from_env = os.environ.get(fallback)
    if from_env:
        return from_env, "env"
    stored = _keyring_get(provider)
    if stored:
        return stored, "keychain"
    return None, "none"


def resolve_api_key(provider: str) -> str:
    key, _source = peek_api_key(provider)
    if key is None:
        env_var = PROVIDER_ENV_VARS[provider]
        msg = (
            f"no API key for provider {provider!r}: set {env_var} or run "
            f"`fde-providers login {provider}`"
        )
        raise CredentialError(msg)
    return key


def store_api_key(provider: str, key: str) -> None:
    _require_known(provider)
    _keyring_set(provider, key)


def delete_api_key(provider: str) -> bool:
    _require_known(provider)
    return _keyring_delete(provider)
