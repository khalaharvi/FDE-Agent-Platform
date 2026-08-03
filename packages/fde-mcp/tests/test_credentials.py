"""Unit tests for fde_mcp.credentials -- resolution order env > keychain > error.

No real keyring backend: `_keyring_get`/`_keyring_set`/`_keyring_delete` are
monkeypatched with a dict-backed fake, which is exactly the boundary the
module draws around the keyring library.
"""

from __future__ import annotations

import pytest

from fde_mcp import credentials
from fde_mcp.config import get_settings


@pytest.fixture
def _clear_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    """Clear the settings cache before and after each test that touches FDE_MODEL_API_KEY."""
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def fake_keychain(monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    store: dict[str, str] = {}

    def _get(account: str) -> str | None:
        return store.get(account)

    def _set(account: str, key: str) -> None:
        store[account] = key

    def _delete(account: str) -> bool:
        return store.pop(account, None) is not None

    monkeypatch.setattr(credentials, "_keyring_get", _get)
    monkeypatch.setattr(credentials, "_keyring_set", _set)
    monkeypatch.setattr(credentials, "_keyring_delete", _delete)
    return store


def test_env_var_wins_over_keychain(
    monkeypatch: pytest.MonkeyPatch, fake_keychain: dict[str, str]
) -> None:
    fake_keychain["anthropic"] = "from-keychain"
    monkeypatch.setenv("ANTHROPIC_API_KEY", "from-env")
    key, source = credentials.peek_api_key("anthropic")
    assert (key, source) == ("from-env", "env")
    assert credentials.resolve_api_key("anthropic") == "from-env"


def test_keychain_used_when_env_unset(
    monkeypatch: pytest.MonkeyPatch, fake_keychain: dict[str, str]
) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    fake_keychain["openai"] = "sk-keychain"
    key, source = credentials.peek_api_key("openai")
    assert (key, source) == ("sk-keychain", "keychain")


def test_missing_everywhere_raises_with_fix(
    monkeypatch: pytest.MonkeyPatch, fake_keychain: dict[str, str]
) -> None:
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    with pytest.raises(credentials.CredentialError) as exc:
        credentials.resolve_api_key("gemini")
    assert "GEMINI_API_KEY" in str(exc.value)
    assert "fde-providers login gemini" in str(exc.value)
    assert credentials.peek_api_key("gemini") == (None, "none")


def test_gemini_accepts_google_api_key_fallback(
    monkeypatch: pytest.MonkeyPatch, fake_keychain: dict[str, str]
) -> None:
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.setenv("GOOGLE_API_KEY", "g-key")
    assert credentials.resolve_api_key("gemini") == "g-key"


def test_unknown_provider_raises(fake_keychain: dict[str, str]) -> None:
    with pytest.raises(credentials.CredentialError):
        credentials.resolve_api_key("netscape")


@pytest.mark.usefixtures("_clear_settings")
def test_store_and_delete_roundtrip(
    monkeypatch: pytest.MonkeyPatch, fake_keychain: dict[str, str]
) -> None:
    monkeypatch.delenv("FDE_MODEL_API_KEY", raising=False)
    credentials.store_api_key("openai-compat", "local-key")
    assert credentials.peek_api_key("openai-compat") == ("local-key", "keychain")
    assert credentials.delete_api_key("openai-compat") is True
    assert credentials.delete_api_key("openai-compat") is False


def test_keyring_delete_handles_no_keyring_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify delete_api_key returns False when keyring backend raises NoKeyringError.

    Does NOT use fake_keychain fixture so the REAL _keyring_delete runs and exercises
    the KeyringError exception handler.
    """
    import keyring  # noqa: PLC0415
    import keyring.errors  # noqa: PLC0415

    def _raise_no_keyring(service: str, account: str) -> None:
        raise keyring.errors.NoKeyringError("no backend")

    monkeypatch.setattr(keyring, "delete_password", _raise_no_keyring)
    assert credentials.delete_api_key("openai") is False
