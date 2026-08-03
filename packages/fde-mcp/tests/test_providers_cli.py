"""Tests for the fde-providers CLI. Network is faked by monkeypatching
`providers_cli._http_get_status`; the keychain by the same seam as
test_credentials.py.
"""

from __future__ import annotations

import pytest

from fde_mcp import credentials, providers_cli


@pytest.fixture
def fake_keychain(monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    store: dict[str, str] = {}
    monkeypatch.setattr(credentials, "_keyring_get", store.get)
    monkeypatch.setattr(credentials, "_keyring_set", store.__setitem__)
    monkeypatch.setattr(
        credentials, "_keyring_delete", lambda account: store.pop(account, None) is not None
    )
    return store


def test_validate_ok_and_bad(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(providers_cli, "_http_get_status", lambda url, headers: 200)
    assert providers_cli.validate_api_key("openai", "sk-x") is None
    monkeypatch.setattr(providers_cli, "_http_get_status", lambda url, headers: 401)
    err = providers_cli.validate_api_key("openai", "sk-bad")
    assert err is not None
    assert "401" in err


def test_validate_compat_requires_base_url() -> None:
    err = providers_cli.validate_api_key("openai-compat", "k")
    assert err is not None
    assert "FDE_MODEL_BASE_URL" in err


def test_login_validates_before_storing(
    monkeypatch: pytest.MonkeyPatch,
    fake_keychain: dict[str, str],
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(providers_cli, "_prompt_secret", lambda prompt: "sk-good")
    monkeypatch.setattr(providers_cli, "_http_get_status", lambda url, headers: 200)
    assert providers_cli.main(["login", "openai"]) == 0
    assert fake_keychain["openai"] == "sk-good"


def test_login_rejects_bad_key_without_storing(
    monkeypatch: pytest.MonkeyPatch,
    fake_keychain: dict[str, str],
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(providers_cli, "_prompt_secret", lambda prompt: "sk-bad")
    monkeypatch.setattr(providers_cli, "_http_get_status", lambda url, headers: 401)
    assert providers_cli.main(["login", "openai"]) == 1
    assert "openai" not in fake_keychain


def test_login_unknown_provider_returns_2_without_prompting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _boom(prompt: str) -> str:
        raise AssertionError("_prompt_secret should not be called for an unknown provider")

    monkeypatch.setattr(providers_cli, "_prompt_secret", _boom)
    assert providers_cli.main(["login", "netscape"]) == 2


def test_logout_unknown_provider_returns_2(
    fake_keychain: dict[str, str],
) -> None:
    assert providers_cli.main(["logout", "netscape"]) == 2


def test_logout_removes_stored_key(
    fake_keychain: dict[str, str],
) -> None:
    fake_keychain["openai"] = "sk-stored"
    assert providers_cli.main(["logout", "openai"]) == 0
    assert "openai" not in fake_keychain


def test_logout_known_provider_with_nothing_stored(
    fake_keychain: dict[str, str],
) -> None:
    assert providers_cli.main(["logout", "openai"]) == 0


def test_status_lists_all_providers(
    monkeypatch: pytest.MonkeyPatch,
    fake_keychain: dict[str, str],
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    monkeypatch.delenv("FDE_MODEL_API_KEY", raising=False)
    fake_keychain["anthropic"] = "sk-a"
    assert providers_cli.main(["status", "--no-validate"]) == 0
    out = capsys.readouterr().out
    assert "anthropic" in out
    assert "keychain" in out
    assert "openai" in out
    assert "none" in out
    assert "bedrock" in out  # reported via AWS chain, not a key


def test_help_and_unknown_subcommand(capsys: pytest.CaptureFixture[str]) -> None:
    assert providers_cli.main(["--help"]) == 0
    assert "login" in capsys.readouterr().out
    assert providers_cli.main(["frobnicate"]) == 2
