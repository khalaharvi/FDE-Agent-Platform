# Multi-Provider LLM Support Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Users can log in to Anthropic/OpenAI/Gemini/any-OpenAI-compatible provider (`fde-providers login`) and run the full propose→gate→merge flow locally via a new `fde-agents-local` driver, with Bedrock remaining the untouched default.

**Architecture:** A provider registry in `fde-agents` (`build_model()` factory replacing the hardcoded `BedrockModel` at `runtime.py:392`), a keychain-backed credential resolver + `fde-providers` CLI in `fde-mcp` (dependency direction already flows fde-agents→fde-mcp), provider-dispatched embeddings in `fde_mcp/embeddings.py`, and a local runner that drives the existing directly-awaitable agent `handler`. Spec: `docs/superpowers/specs/2026-08-02-multi-provider-llm-design.md` (committed on this branch).

**Tech Stack:** Strands `AnthropicModel`/`OpenAIModel`/`GeminiModel` (extras: `strands-agents[anthropic,openai,gemini]` — extra names verified in dist-info), `keyring`, `httpx` (already transitive via `mcp`; made explicit).

## Global Constraints

- Branch: `feat/multi-provider-llm` (already exists, spec committed). PR at the end; **the user merges — never merge, never enable auto-merge** (main is protected: 9 required checks, admins included).
- Every module: `from __future__ import annotations` (ruff-enforced). mypy `--strict` covers fde-mcp and fde-agents src. `ruff check` + `ruff format` must pass.
- New env vars go in the package's `config.py`, nowhere else (docs/11 §10). Frozen dataclasses + `from_env()` + the existing `_env_str`/`_env_opt_str` helpers; tests `cache_clear()` the settings caches (fde-agents conftest already autouse-clears both).
- Optional/heavy imports are function-local with `# noqa: PLC0415` (existing convention: `embeddings.py:132`).
- New deps via `uv add --package <member>` then `uv sync --all-packages`; commit `uv.lock`. Never bare `uv lock`.
- Test module basenames must be unique across ALL packages' tests dirs. New names (verified unique): `test_providers.py`, `test_credentials.py`, `test_providers_cli.py`, `test_embeddings_providers.py`, `test_local_runner.py`.
- Root pytest addopts already has `-q` — never add another. DB-marked tests need `FDE_DB_DSN`; none of the new tests touch the DB or the network (stub HTTP servers on 127.0.0.1 are fine; they are in-test, not external).
- NO schema changes; NO grants; the 8 privilege-denial invariants and `test_parity.py` must be untouched. Embeddings stay 1024-dim (`vector(1024)` domain).
- No silent fallbacks anywhere: wrong/missing config raises with the fix named in the message (repo precedent: `resolve_model_id`'s preset-typo error; fake-embeddings incident docs/99 §7).
- Run full gates before the PR: `uv run pytest packages`, `uv run ruff check packages && uv run ruff format --check packages`, `uv run mypy`, `uv lock --check`.

---

### Task 1: Credential resolver in fde-mcp

**Files:**
- Create: `packages/fde-mcp/src/fde_mcp/credentials.py`
- Test: `packages/fde-mcp/tests/test_credentials.py`
- Modify: `packages/fde-mcp/pyproject.toml` (deps: `keyring>=25`, `httpx>=0.27`)

**Interfaces:**
- Produces: `resolve_api_key(provider: str) -> str` (raises `CredentialError`), `peek_api_key(provider: str) -> tuple[str | None, str]` (key-or-None, source ∈ `"env" | "keychain" | "none"`), `store_api_key(provider: str, key: str) -> None`, `delete_api_key(provider: str) -> bool`, `PROVIDER_ENV_VARS: dict[str, str]`, `class CredentialError(RuntimeError)`. Keychain service name constant `KEYCHAIN_SERVICE = "fde-platform"`.

- [ ] **Step 1: Copy plan + add deps.** Copy this plan into the repo as `docs/superpowers/plans/2026-08-02-multi-provider-llm.md`. Then:

```bash
cd "$(git rev-parse --show-toplevel)"
uv add --package fde-mcp "keyring>=25" "httpx>=0.27"
uv sync --all-packages
git add docs/superpowers/plans/2026-08-02-multi-provider-llm.md packages/fde-mcp/pyproject.toml uv.lock
git commit -m "docs: implementation plan for multi-provider LLM; deps: keyring+httpx on fde-mcp"
```

- [ ] **Step 2: Write the failing tests** in `packages/fde-mcp/tests/test_credentials.py`:

```python
"""Unit tests for fde_mcp.credentials -- resolution order env > keychain > error.

No real keyring backend: `_keyring_get`/`_keyring_set`/`_keyring_delete` are
monkeypatched with a dict-backed fake, which is exactly the boundary the
module draws around the keyring library.
"""

from __future__ import annotations

import pytest

from fde_mcp import credentials


@pytest.fixture()
def fake_keychain(monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    store: dict[str, str] = {}
    monkeypatch.setattr(credentials, "_keyring_get", lambda account: store.get(account))
    monkeypatch.setattr(
        credentials, "_keyring_set", lambda account, key: store.__setitem__(account, key)
    )
    monkeypatch.setattr(
        credentials, "_keyring_delete", lambda account: store.pop(account, None) is not None
    )
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


def test_store_and_delete_roundtrip(
    monkeypatch: pytest.MonkeyPatch, fake_keychain: dict[str, str]
) -> None:
    monkeypatch.delenv("FDE_MODEL_API_KEY", raising=False)
    credentials.store_api_key("openai-compat", "local-key")
    assert credentials.peek_api_key("openai-compat") == ("local-key", "keychain")
    assert credentials.delete_api_key("openai-compat") is True
    assert credentials.delete_api_key("openai-compat") is False
```

- [ ] **Step 3: Run to verify failure.** `uv run pytest packages/fde-mcp/tests/test_credentials.py -v` — expect `ModuleNotFoundError`/`AttributeError` collection errors.

- [ ] **Step 4: Implement** `packages/fde-mcp/src/fde_mcp/credentials.py`:

```python
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
    except keyring.errors.PasswordDeleteError:
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
```

- [ ] **Step 5: Run tests → PASS**, then `uv run mypy` and `uv run ruff check packages/fde-mcp`.

- [ ] **Step 6: Commit.** `git add -A && git commit -m "feat: keychain-backed provider credential resolver in fde-mcp"`

---

### Task 2: `fde-providers` CLI (login / logout / status)

**Files:**
- Create: `packages/fde-mcp/src/fde_mcp/providers_cli.py`
- Test: `packages/fde-mcp/tests/test_providers_cli.py`
- Modify: `packages/fde-mcp/pyproject.toml` (`[project.scripts]` add `fde-providers = "fde_mcp.providers_cli:main"`)

**Interfaces:**
- Consumes: Task 1's `credentials` module.
- Produces: `validate_api_key(provider: str, key: str, *, base_url: str | None = None) -> str | None` (None = valid, else error text) — Task 6's `doctor` reuses it. `main(argv: list[str] | None = None) -> int`.

- [ ] **Step 1: Write the failing tests** in `packages/fde-mcp/tests/test_providers_cli.py`:

```python
"""Tests for the fde-providers CLI. Network is faked by monkeypatching
`providers_cli._http_get_status`; the keychain by the same seam as
test_credentials.py.
"""

from __future__ import annotations

import pytest

from fde_mcp import credentials, providers_cli


@pytest.fixture()
def fake_keychain(monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    store: dict[str, str] = {}
    monkeypatch.setattr(credentials, "_keyring_get", lambda account: store.get(account))
    monkeypatch.setattr(
        credentials, "_keyring_set", lambda account, key: store.__setitem__(account, key)
    )
    monkeypatch.setattr(
        credentials, "_keyring_delete", lambda account: store.pop(account, None) is not None
    )
    return store


def test_validate_ok_and_bad(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(providers_cli, "_http_get_status", lambda url, headers: 200)
    assert providers_cli.validate_api_key("openai", "sk-x") is None
    monkeypatch.setattr(providers_cli, "_http_get_status", lambda url, headers: 401)
    err = providers_cli.validate_api_key("openai", "sk-bad")
    assert err is not None and "401" in err


def test_validate_compat_requires_base_url() -> None:
    err = providers_cli.validate_api_key("openai-compat", "k")
    assert err is not None and "FDE_MODEL_BASE_URL" in err


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
    assert "anthropic" in out and "keychain" in out
    assert "openai" in out and "none" in out
    assert "bedrock" in out  # reported via AWS chain, not a key


def test_help_and_unknown_subcommand(capsys: pytest.CaptureFixture[str]) -> None:
    assert providers_cli.main(["--help"]) == 0
    assert "login" in capsys.readouterr().out
    assert providers_cli.main(["frobnicate"]) == 2
```

- [ ] **Step 2: Run → FAIL** (`uv run pytest packages/fde-mcp/tests/test_providers_cli.py -v`).

- [ ] **Step 3: Implement** `packages/fde-mcp/src/fde_mcp/providers_cli.py`. Hand-rolled `--help` (repo convention: it is the first command a new developer types — CLAUDE.md). Key parts:

```python
"""fde-providers -- log in to LLM providers, check what's configured.

Subcommands:
  login <provider>    prompt for an API key, validate it live, store in the
                      OS keychain (service "fde-platform")
  logout <provider>   remove the stored key
  status [--no-validate]
                      one row per provider: source (env/keychain/none) and,
                      unless --no-validate, a live key check

Providers: anthropic, openai, gemini, openai-compat (openai-compat
validates against FDE_MODEL_BASE_URL). Bedrock needs no key here -- it uses
the ambient AWS credential chain; `status` reports whether one is present.
"""

from __future__ import annotations

import getpass
import os
import sys

from fde_mcp.credentials import (
    PROVIDER_ENV_VARS,
    delete_api_key,
    peek_api_key,
    store_api_key,
)

_USAGE = __doc__ or ""

# Cheapest authenticated GET per provider. 2xx = key works.
_VALIDATION_URLS: dict[str, tuple[str, dict[str, str]]] = {
    "anthropic": (
        "https://api.anthropic.com/v1/models",
        {"x-api-key": "{key}", "anthropic-version": "2023-06-01"},
    ),
    "openai": ("https://api.openai.com/v1/models", {"Authorization": "Bearer {key}"}),
    "gemini": (
        "https://generativelanguage.googleapis.com/v1beta/models?key={key}",
        {},
    ),
}


def _http_get_status(url: str, headers: dict[str, str]) -> int:
    import httpx  # noqa: PLC0415 -- keep httpx optional until first use

    return httpx.get(url, headers=headers, timeout=15.0).status_code


def _prompt_secret(prompt: str) -> str:
    return getpass.getpass(prompt)


def validate_api_key(provider: str, key: str, *, base_url: str | None = None) -> str | None:
    """None if the key authenticates; else a one-line human explanation."""
    if provider == "openai-compat":
        url = base_url or os.environ.get("FDE_MODEL_BASE_URL")
        if not url:
            return "openai-compat needs FDE_MODEL_BASE_URL set to validate against"
        target, headers = (f"{url.rstrip('/')}/models", {"Authorization": f"Bearer {key}"})
    elif provider in _VALIDATION_URLS:
        raw_url, raw_headers = _VALIDATION_URLS[provider]
        target = raw_url.replace("{key}", key)
        headers = {h: v.replace("{key}", key) for h, v in raw_headers.items()}
    else:
        return f"unknown provider {provider!r}"
    try:
        status = _http_get_status(target, headers)
    except Exception as exc:  # noqa: BLE001 -- report, don't crash a CLI
        return f"validation request failed: {exc}"
    if 200 <= status < 300:
        return None
    return f"provider returned HTTP {status} (bad key?)"
```

`main(argv)` dispatches `login`/`logout`/`status`/`--help`; `login` prompts via `_prompt_secret`, calls `validate_api_key`, prints the error and returns 1 **without storing** on failure, else `store_api_key` and returns 0. `status` iterates `sorted(PROVIDER_ENV_VARS) + ["bedrock"]`; for bedrock it reports `env` if any of `AWS_ACCESS_KEY_ID`/`AWS_PROFILE`/`AWS_ROLE_ARN` is set else `none (uses AWS credential chain)`; unknown subcommand prints usage to stderr and returns 2. Add the console script line to `packages/fde-mcp/pyproject.toml`.

- [ ] **Step 4: Run tests → PASS**; `uv sync --all-packages` (re-installs entry points); smoke `uv run fde-providers --help`.
- [ ] **Step 5: mypy + ruff clean. Commit:** `git commit -m "feat: fde-providers CLI -- validated login, logout, status"`

---

### Task 3: Provider registry + model-backend settings (fde-agents)

**Files:**
- Create: `packages/fde-agents/src/fde_agents/common/providers.py`
- Test: `packages/fde-agents/tests/test_providers.py`
- Modify: `packages/fde-agents/src/fde_agents/common/config.py` (add `ModelBackendSettings`; extend `AgentRuntimeSettings` + `resolve_model_id`)
- Modify: `packages/fde-agents/pyproject.toml` (`strands-agents>=1.50` → `strands-agents[anthropic,openai,gemini]>=1.50`)

**Interfaces:**
- Consumes: `fde_mcp.credentials.resolve_api_key`.
- Produces:
  - `config.ModelBackendSettings` (frozen dataclass): `provider: str` (`FDE_MODEL_PROVIDER`, default `"bedrock"`), `base_url: str | None` (`FDE_MODEL_BASE_URL`), `compat_preset: str | None` (`FDE_MODEL_COMPAT_PRESET`), `max_tokens: int` (`FDE_MODEL_MAX_TOKENS`, default 8192 — Anthropic's API requires an explicit max_tokens).
  - `AgentRuntimeSettings.model_backend: ModelBackendSettings` (from_env wired in).
  - `providers.build_model(model_id: str, backend: ModelBackendSettings) -> Any` (returns a strands `Model`; typed `Any`-free via the classes' own types).
  - `providers.qualified_model_id(provider: str, model_id: str) -> str` (`"bedrock"` → bare id; else `f"{provider}/{model_id}"`).
  - `providers.resolve_base_url(backend) -> str` (explicit `base_url` wins over `COMPAT_PRESETS[compat_preset]`; raises `ValueError` if neither).
  - `providers.PROVIDER_KEYS: frozenset[str]`, `providers.COMPAT_PRESETS: dict[str, str]` = `{"opencode-zen": "https://opencode.ai/zen/v1", "ollama": "http://127.0.0.1:11434/v1", "lmstudio": "http://127.0.0.1:1234/v1"}`.

- [ ] **Step 1: Dependency.** `uv add --package fde-agents "strands-agents[anthropic,openai,gemini]>=1.50" && uv sync --all-packages` (extra names verified against the installed dist-info). Commit with the lockfile.

- [ ] **Step 2: Write the failing tests** in `packages/fde-agents/tests/test_providers.py`:

```python
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
    b2 = _backend(
        provider="openai-compat", compat_preset="opencode-zen", base_url="http://x:1/v1"
    )
    assert providers.resolve_base_url(b2) == "http://x:1/v1"
    model = providers.build_model("qwen3-coder", b2)
    assert model.kwargs["client_args"] == {"api_key": "key-openai-compat", "base_url": "http://x:1/v1"}


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
```

Plus config-level tests appended to the existing `packages/fde-agents/tests/test_model_presets.py` (it already covers `resolve_model_id`):

```python
def test_non_bedrock_provider_requires_explicit_model_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FDE_MODEL_PROVIDER", "anthropic")
    monkeypatch.delenv("FDE_MODEL_ID", raising=False)
    _clear_settings_caches()  # use this module's existing cache-clear helper/fixture
    with pytest.raises(ValueError, match="FDE_MODEL_ID"):
        resolve_model_id("engagement")


def test_non_bedrock_provider_with_explicit_model_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FDE_MODEL_PROVIDER", "anthropic")
    monkeypatch.setenv("FDE_MODEL_ID", "claude-sonnet-5")
    _clear_settings_caches()
    assert resolve_model_id("engagement") == "claude-sonnet-5"
```

(Adopt whatever cache-clearing helper `test_model_presets.py` already uses — read it first; do not invent a second pattern.)

- [ ] **Step 3: Run → FAIL.**
- [ ] **Step 4: Implement.** In `config.py`, after `GatewaySettings`:

```python
@dataclass(frozen=True, slots=True)
class ModelBackendSettings:
    """Which inference backend authors agent turns, and how to reach it.

    Attributes:
        provider: `FDE_MODEL_PROVIDER`, default "bedrock". One of
            providers.PROVIDER_KEYS; validated in providers.build_model
            (not here) so the error can name every valid value.
        base_url: `FDE_MODEL_BASE_URL`. openai-compat endpoint; presence
            wins over `compat_preset`.
        compat_preset: `FDE_MODEL_COMPAT_PRESET`. Named openai-compat
            endpoint (see providers.COMPAT_PRESETS).
        max_tokens: `FDE_MODEL_MAX_TOKENS`, default 8192. Anthropic's
            Messages API requires an explicit ceiling; other providers
            use their own defaults and ignore this.
    """

    provider: str
    base_url: str | None
    compat_preset: str | None
    max_tokens: int

    @classmethod
    def from_env(cls) -> ModelBackendSettings:
        return cls(
            provider=_env_str("FDE_MODEL_PROVIDER", "bedrock"),
            base_url=_env_opt_str("FDE_MODEL_BASE_URL"),
            compat_preset=_env_opt_str("FDE_MODEL_COMPAT_PRESET"),
            max_tokens=_env_int("FDE_MODEL_MAX_TOKENS", 8192),
        )
```

Extend `AgentRuntimeSettings` with `model_backend: ModelBackendSettings` (+ `from_env`). In `resolve_model_id`, before the preset logic:

```python
    provider = ModelBackendSettings.from_env().provider  # cheap; no cache interplay
    if provider != "bedrock":
        if explicit:
            return explicit
        msg = (
            f"FDE_MODEL_PROVIDER={provider!r} requires an explicit FDE_MODEL_ID "
            "(MODEL_PRESETS name Bedrock model ids, which mean nothing to other "
            "providers)"
        )
        raise ValueError(msg)
```

(`explicit` is the existing `get_settings().agent.model_id` read — keep that line first.) Then `providers.py`:

```python
"""providers.py -- the model-backend registry: one factory, five backends.

`build_model` is the only place a strands model class is constructed for
agent turns (runtime.py calls it where `BedrockModel(...)` used to be
hardcoded). Provider SDK imports are function-local so `bedrock` deployments
never import anthropic/openai/google-genai.
"""

from __future__ import annotations

from typing import Any

from fde_agents.common.config import ModelBackendSettings

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
        return _openai_cls()(
            client_args={"api_key": _resolve_api_key(provider)}, model_id=model_id
        )
    if provider == "gemini":
        return _gemini_cls()(
            client_args={"api_key": _resolve_api_key(provider)}, model_id=model_id
        )
    if provider == "openai-compat":
        return _openai_cls()(
            client_args={
                "api_key": _resolve_api_key(provider),
                "base_url": resolve_base_url(backend),
            },
            model_id=model_id,
        )
    msg = f"FDE_MODEL_PROVIDER={provider!r} invalid; choose one of {sorted(PROVIDER_KEYS)}"
    raise ValueError(msg)
```

Note: `openai-compat` key resolution must tolerate keyless local servers — `_resolve_api_key("openai-compat")` raises if nothing is set, so ALSO catch that one case and default to `"local"` (Ollama/LM Studio ignore the key; the OpenAI client requires a non-empty string). Implement inside `build_model`'s compat branch:

```python
        try:
            key = _resolve_api_key(provider)
        except Exception:  # CredentialError; keyless local servers are fine
            key = "local"
```

…and import `CredentialError` properly rather than a blind except: `from fde_mcp.credentials import CredentialError` at module top (fde-mcp is a hard dep; this import is cheap and typed).

- [ ] **Step 5: Run tests → PASS; mypy + ruff. Commit:** `git commit -m "feat: provider registry + model-backend settings in fde-agents"`

---

### Task 4: Wire the factory into runtime.py + audit-qualified model id

**Files:**
- Modify: `packages/fde-agents/src/fde_agents/common/runtime.py` (imports; lines 367-393)
- Modify: `packages/fde-agents/tests/test_runtime.py` (fixture at lines 63-66; one new test)

**Interfaces:**
- Consumes: `providers.build_model`, `providers.qualified_model_id`, `settings.model_backend`.
- Produces: unchanged runtime behavior for bedrock; `tracing.start_session(model_id=...)` now receives the qualified id.

- [ ] **Step 1: Adapt the autouse fixture first (it will fail until Step 2 — that's the failing-test state).** In `test_runtime.py` replace lines 63-66 with:

```python
@pytest.fixture(autouse=True)
def _patch_agent_and_model(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(rt, "Agent", _FakeAgent)
    monkeypatch.setattr(
        rt.providers, "build_model", lambda model_id, backend: model_id
    )
```

Add one new test asserting the audit id is qualified:

```python
@pytest.mark.anyio
async def test_start_session_records_qualified_model_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FDE_MODEL_PROVIDER", "anthropic")
    monkeypatch.setenv("FDE_MODEL_ID", "claude-sonnet-5")
    # conftest's autouse fixture clears both settings caches around each test
    starts: list[dict[str, Any]] = []

    async def _fake_start_session(mcp_client: Any, **kwargs: Any) -> Any:
        starts.append(kwargs)
        return object()

    monkeypatch.setattr(rt.tracing, "start_session", _fake_start_session)
    # ... reuse this file's existing _patch_mcp_client/_patch_turn/_collect
    # helpers and end_session stub to drive one successful "do_thing" task,
    # exactly as the neighbouring outcome tests do ...
    assert starts[0]["model_id"] == "anthropic/claude-sonnet-5"
```

(Mirror the async test style already used in this file — copy the setup lines from the nearest passing test rather than inventing a new harness. If the file's tests are sync-driven via an event-loop helper, follow that instead of `anyio`.)

- [ ] **Step 2: Implement in runtime.py.** Remove `from strands.models.bedrock import BedrockModel` (line 49) and add `from fde_agents.common import providers`. Replace lines 367-370 + 392:

```python
    # Resolved once, used for both the audit column and the actual model
    # call -- the proposal's recorded model id and the model that authored
    # it can never disagree (see config.MODEL_PRESETS / providers.py).
    model_id = resolve_model_id(config.agent_key)
    backend = settings.model_backend
```

…start_session's `model_id=` argument becomes `providers.qualified_model_id(backend.provider, model_id)`, and line 392 becomes:

```python
        model = providers.build_model(model_id, backend)
```

- [ ] **Step 3: Full fde-agents suite → PASS** (`uv run pytest packages/fde-agents -v`), mypy, ruff.
- [ ] **Step 4: Commit:** `git commit -m "feat: agent runtime builds its model through the provider registry"`

---

### Task 5: Multi-provider embeddings (fde-mcp)

**Files:**
- Modify: `packages/fde-mcp/src/fde_mcp/config.py` (`EmbeddingSettings`: add `provider`, `base_url`, `api_key` fields)
- Modify: `packages/fde-mcp/src/fde_mcp/embeddings.py` (provider dispatch + HTTP paths + per-vector dim check)
- Test: `packages/fde-mcp/tests/test_embeddings_providers.py`

**Interfaces:**
- Consumes: `credentials.resolve_api_key` (openai/gemini; compat falls back to `"local"` like Task 3).
- Produces: `embed_batch`/`embed` signatures unchanged (call sites in `tools/graph.py:175`, `embedder_worker.py:315/323/330`, `rollout_env.py:206` untouched). New settings: `FDE_EMBED_PROVIDER` (default `"bedrock"`), `FDE_EMBED_BASE_URL`, `FDE_EMBED_API_KEY`. New module constant `GEMINI_EMBED_BASE = "https://generativelanguage.googleapis.com/v1beta"` (monkeypatchable in tests).

- [ ] **Step 1: Write the failing tests.** IMPORTANT: fde-mcp's conftest has a session-scoped autouse `_fake_bedrock` fixture that replaces `embeddings.embed`/`embed_batch` module attributes. Capture the real functions at import time (collection runs before fixtures):

```python
"""Provider-path tests for fde_mcp.embeddings against in-test HTTP stubs.

Module-level captures below grab the REAL functions during collection,
before conftest's session-scoped `_fake_bedrock` fixture replaces the
module attributes -- these tests exercise the actual HTTP provider paths.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import pytest

from fde_mcp import embeddings
from fde_mcp.config import get_settings

_REAL_EMBED_BATCH = embeddings.embed_batch

DIMS = 1024


def _vec(seed: float) -> list[float]:
    return [seed] * DIMS


class _EmbedStub(BaseHTTPRequestHandler):
    """Answers POST like an OpenAI /v1/embeddings endpoint; records requests."""

    requests: list[dict[str, Any]] = []
    dims = DIMS
    fail_first_with: int | None = None
    _failed_once = False

    def do_POST(self) -> None:  # noqa: N802 -- http.server naming
        cls = type(self)
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        cls.requests.append({"path": self.path, "auth": self.headers.get("Authorization"), "body": body})
        if cls.fail_first_with and not cls._failed_once:
            cls._failed_once = True
            self.send_response(cls.fail_first_with)
            self.end_headers()
            return
        texts = body["input"] if isinstance(body["input"], list) else [body["input"]]
        payload = {"data": [{"embedding": _vec(0.5)[: cls.dims], "index": i} for i in range(len(texts))]}
        raw = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, *args: Any) -> None:  # silence test output
        return


@pytest.fixture()
def stub_server() -> Any:
    _EmbedStub.requests = []
    _EmbedStub.dims = DIMS
    _EmbedStub.fail_first_with = None
    _EmbedStub._failed_once = False
    server = ThreadingHTTPServer(("127.0.0.1", 0), _EmbedStub)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}/v1"
    server.shutdown()


@pytest.fixture()
def _compat_env(monkeypatch: pytest.MonkeyPatch, stub_server: str) -> None:
    monkeypatch.setenv("FDE_EMBED_PROVIDER", "openai-compat")
    monkeypatch.setenv("FDE_EMBED_BASE_URL", stub_server)
    monkeypatch.setenv("FDE_EMBED_MODEL_ID", "test-embed")
    monkeypatch.setenv("FDE_EMBED_API_KEY", "k-compat")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


async def test_compat_path_shape_and_roundtrip(_compat_env: None) -> None:
    vecs = await _REAL_EMBED_BATCH(["alpha", "beta"], input_type="search_document")
    assert len(vecs) == 2 and all(len(v) == DIMS for v in vecs)
    req = _EmbedStub.requests[0]
    assert req["path"].endswith("/embeddings")
    assert req["auth"] == "Bearer k-compat"
    assert req["body"]["model"] == "test-embed"
    assert req["body"]["input"] == ["alpha", "beta"]


async def test_openai_path_sends_dimensions(
    monkeypatch: pytest.MonkeyPatch, stub_server: str
) -> None:
    monkeypatch.setenv("FDE_EMBED_PROVIDER", "openai")
    monkeypatch.setenv("FDE_EMBED_MODEL_ID", "text-embedding-3-small")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-o")
    monkeypatch.setattr(embeddings, "OPENAI_EMBED_URL", f"{stub_server}/embeddings")
    get_settings.cache_clear()
    try:
        await _REAL_EMBED_BATCH(["gamma"])
        assert _EmbedStub.requests[0]["body"]["dimensions"] == DIMS
    finally:
        get_settings.cache_clear()


async def test_wrong_dims_raises_actionably(_compat_env: None) -> None:
    _EmbedStub.dims = 768
    with pytest.raises(ValueError, match="768"):
        await _REAL_EMBED_BATCH(["delta-768"])


async def test_retries_on_429_then_succeeds(
    _compat_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FDE_EMBED_BASE_BACKOFF", "0.01")
    get_settings.cache_clear()
    _EmbedStub.fail_first_with = 429
    vecs = await _REAL_EMBED_BATCH(["epsilon-retry"])
    assert len(vecs) == 1 and len(_EmbedStub.requests) == 2
```

(Also add a `gemini` request-shape test: monkeypatch `embeddings.GEMINI_EMBED_BASE` to the stub and extend `_EmbedStub.do_POST` to answer `:batchEmbedContents` paths with `{"embeddings": [{"values": [...]}, ...]}`, asserting the request carries `output_dimensionality: 1024` per item. Use distinct input texts in every test — the module-level LRU cache is process-wide.)

- [ ] **Step 2: Run → FAIL.**
- [ ] **Step 3: Implement.** `EmbeddingSettings` gains (with docstring entries following the file's style):

```python
    provider: str        # FDE_EMBED_PROVIDER, default "bedrock"
    base_url: str | None  # FDE_EMBED_BASE_URL (openai-compat only)
    api_key: str | None   # FDE_EMBED_API_KEY (compat override; else credentials.py)
```

In `embeddings.py`: module constants `OPENAI_EMBED_URL = "https://api.openai.com/v1/embeddings"`, `GEMINI_EMBED_BASE = "https://generativelanguage.googleapis.com/v1beta"`, `GEMINI_MAX_BATCH = 100`. New helpers:

- `_check_dims(vec, model_id) -> list[float]`: raises `ValueError` naming actual vs expected dims, the model id, and "known 1024-dim choices: text-embedding-3-small/-large (dimensions=1024), gemini-embedding-001 (output_dimensionality=1024), mxbai-embed-large, bge-m3".
- `async _http_post_with_retry(url, headers, body: dict) -> dict`: httpx (function-local import) `AsyncClient.post`, retrying status 429/500/502/503/504 with the existing `max_retries`/`base_backoff_seconds`/`max_backoff_seconds` knobs + jitter (mirror `_invoke_with_retry`'s loop shape); non-2xx after retries raises `ValueError` with status + body snippet.
- `_embed_api_key() -> str`: `settings.api_key` if set, else `credentials.resolve_api_key(provider)` for openai/gemini; for openai-compat fall back to `"local"` on `CredentialError` (same rationale as Task 3).
- `async _openai_style_uncached(texts, model_id, *, url, api_key, send_dimensions: bool) -> list[list[float]]`: one POST `{"model": model_id, "input": texts, **({"dimensions": dims} if send_dimensions else {})}` with `Authorization: Bearer`, parse `sorted(payload["data"], key=itemgetter("index"))` → `[_check_dims(d["embedding"], model_id) for d in ...]`, length-checked against input like the Cohere path.
- `async _gemini_uncached(texts, model_id) -> list[list[float]]`: chunks of `GEMINI_MAX_BATCH`, POST `f"{GEMINI_EMBED_BASE}/models/{model_id}:batchEmbedContents?key={api_key}"` with `{"requests": [{"model": f"models/{model_id}", "content": {"parts": [{"text": t}]}, "output_dimensionality": dims} for t in chunk]}`, parse `payload["embeddings"][i]["values"]` through `_check_dims`.

In `embed_batch`, replace the `if uncached_idx:` body's dispatch with:

```python
    if uncached_idx:
        provider = get_settings().embedding.provider
        uncached_texts = [texts[i] for i in uncached_idx]
        if provider == "bedrock":
            ...  # existing Titan/Cohere/else block, verbatim, filling results/cache as today
        elif provider in {"openai", "openai-compat", "gemini"}:
            if provider == "gemini":
                vecs = await _gemini_uncached(uncached_texts, resolved_model_id)
            else:
                settings = get_settings().embedding
                if provider == "openai":
                    url, send_dims = OPENAI_EMBED_URL, True
                else:
                    if not settings.base_url:
                        msg = "FDE_EMBED_PROVIDER=openai-compat requires FDE_EMBED_BASE_URL"
                        raise ValueError(msg)
                    url, send_dims = settings.base_url.rstrip("/") + "/embeddings", False
                vecs = await _openai_style_uncached(
                    uncached_texts, resolved_model_id,
                    url=url, api_key=_embed_api_key(), send_dimensions=send_dims,
                )
            for i, vec in zip(uncached_idx, vecs, strict=True):
                results[i] = vec
                await _cache_put(keys[i], vec)
        else:
            msg = (
                f"FDE_EMBED_PROVIDER={provider!r} invalid; choose one of "
                "['bedrock', 'gemini', 'openai', 'openai-compat']"
            )
            raise ValueError(msg)
```

Also: `_cache_key` gains the provider as a fourth tuple element (`(provider, model_id, input_type, digest)`) so switching providers never serves a stale vector; update the docstring at lines 80-86 and the `embed()` docstring to note `input_type` is Bedrock/Cohere-only (HTTP providers have no such field). Update the module docstring's first paragraph to describe the four providers.

- [ ] **Step 4: Run the new tests AND the whole fde-mcp suite → PASS** (proves the conftest `_fake_bedrock` interplay and existing graph/evidence tools are unaffected). mypy + ruff.
- [ ] **Step 5: Commit:** `git commit -m "feat: openai/gemini/openai-compat embedding providers with 1024-dim enforcement"`

---

### Task 6: `fde-agents-local` driver + doctor

**Files:**
- Create: `packages/fde-agents/src/fde_agents/local_runner.py`
- Test: `packages/fde-agents/tests/test_local_runner.py`
- Modify: `packages/fde-agents/pyproject.toml` (`[project.scripts]` add `fde-agents-local = "fde_agents.local_runner:main"`)

**Interfaces:**
- Consumes: each agent module's module-level `handler` (`fde_agents.<agent>.agent.handler`, an async generator `(payload, context=None) -> AsyncIterator[StreamEvent]` — see `engagement/agent.py:120`); `fde_mcp.providers_cli.validate_api_key`; `fde_mcp.credentials.peek_api_key`; `config.ModelBackendSettings`.
- Produces: `main(argv: list[str] | None = None) -> int`; `_load_handler(agent: str) -> Handler`; `run(agent, task, engagement_id, task_input, *, as_json) -> int`.

- [ ] **Step 1: Write the failing tests** in `packages/fde-agents/tests/test_local_runner.py`:

```python
"""Tests for the fde-agents-local driver. The agent handler is faked by
monkeypatching `local_runner._load_handler`; nothing here touches a DB,
a model provider, or MCP.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

import pytest

from fde_agents import local_runner


def _fake_handler(events: list[dict[str, Any]]) -> Any:
    captured: dict[str, Any] = {}

    async def handler(payload: dict[str, Any], context: Any = None) -> AsyncIterator[dict[str, Any]]:
        captured["payload"] = payload
        for event in events:
            yield event

    return handler, captured


def test_run_streams_events_and_builds_payload(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    handler, captured = _fake_handler(
        [{"type": "status", "message": "starting"}, {"type": "done", "outcome": "accepted"}]
    )
    monkeypatch.setattr(local_runner, "_load_handler", lambda agent: handler)
    rc = local_runner.main(
        ["engagement", "--task", "map_process", "--engagement-id", "e-1",
         "--input", '{"process_key": "quote_to_cash"}', "--json"]
    )
    assert rc == 0
    assert captured["payload"] == {
        "task": "map_process",
        "engagement_id": "e-1",
        "input": {"process_key": "quote_to_cash"},
    }
    lines = [json.loads(line) for line in capsys.readouterr().out.strip().splitlines()]
    assert lines[-1] == {"type": "done", "outcome": "accepted"}


def test_error_event_sets_exit_code(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    handler, _ = _fake_handler([{"type": "error", "error": "boom"}])
    monkeypatch.setattr(local_runner, "_load_handler", lambda agent: handler)
    rc = local_runner.main(["workflow", "--task", "t", "--engagement-id", "e-1"])
    assert rc == 1


def test_unknown_agent_and_bad_input_json(capsys: pytest.CaptureFixture[str]) -> None:
    assert local_runner.main(["nonsuch", "--task", "t", "--engagement-id", "e"]) == 2
    assert (
        local_runner.main(
            ["engagement", "--task", "t", "--engagement-id", "e", "--input", "{not json"]
        )
        == 2
    )


def test_help_mentions_doctor(capsys: pytest.CaptureFixture[str]) -> None:
    assert local_runner.main(["--help"]) == 0
    out = capsys.readouterr().out
    assert "doctor" in out and "--engagement-id" in out


def test_doctor_reports_each_check(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(
        local_runner,
        "_doctor_checks",
        lambda: [("db", None), ("model provider", "no key: run fde-providers login openai")],
    )
    rc = local_runner.main(["doctor"])
    assert rc == 1
    out = capsys.readouterr().out
    assert "db" in out and "fde-providers login openai" in out
```

- [ ] **Step 2: Run → FAIL.**
- [ ] **Step 3: Implement** `local_runner.py`. Module docstring is the hand-rolled `--help` text (same pattern as `fde-providers`), covering usage, the three agent names, the engagement task names as examples (`map_process`, `score_opportunities`, `detect_bottlenecks`, `ingest_interview` — from `engagement/agent.py:42`), env prerequisites (`FDE_DB_DSN`; `FDE_MODEL_PROVIDER`+`FDE_MODEL_ID` or Bedrock default; `FDE_EMBED_PROVIDER`), and `doctor`. Core pieces:

```python
_AGENTS = ("engagement", "workflow", "development")


def _load_handler(agent: str) -> Any:
    import importlib  # noqa: PLC0415

    module = importlib.import_module(f"fde_agents.{agent}.agent")
    return module.handler
```

`run(...)` builds `payload = {"task": task, "engagement_id": engagement_id, "input": task_input}`, then `asyncio.run(_drive(handler, payload, as_json))`; `_drive` iterates `handler(payload)`: `--json` prints each event as one JSON line; otherwise human format (`[status] …`, `[error] …`, tool events by type, final `[done] outcome=…`). Track `saw_error = event.get("type") == "error"`; return 1 if any, else 0. Argv parsing is manual (positional agent-or-doctor, then flag pairs) — reject unknown agents/flags/bad JSON with usage to stderr and exit 2. Warn to stderr (not fail) if `FDE_GATEWAY_URL` is set: "local mode expects stdio MCP; unset FDE_GATEWAY_URL".

`_doctor_checks() -> list[tuple[str, str | None]]` (name, problem-or-None), each check isolated:

1. **db** — `FDE_DB_DSN` set? Then `psycopg.connect(dsn, connect_timeout=5)` + `SELECT 1` (function-local import).
2. **model provider** — read `ModelBackendSettings.from_env()`; for bedrock report the AWS chain presence; else `peek_api_key(provider)` + `validate_api_key(provider, key, base_url=resolve_base_url(backend) if provider == "openai-compat" else None)` (imports from `fde_mcp.providers_cli` / `fde_mcp.credentials` / `.common.providers`).
3. **model id** — `resolve_model_id("engagement")` inside try/except reporting the `ValueError` text verbatim (it already names the fix).
4. **embeddings** — `FDE_EMBED_PROVIDER` valid; compat requires `FDE_EMBED_BASE_URL`; non-bedrock non-compat providers get a `peek_api_key` check.
5. **gateway** — warn if `FDE_GATEWAY_URL` set.

`doctor` prints `ok <name>` / `FAIL <name>: <problem>` per check, returns 1 if any failed. Wire the console script; `uv sync --all-packages`.

- [ ] **Step 4: Run tests → PASS; whole fde-agents suite; mypy + ruff.**
- [ ] **Step 5: Manual smoke** (documented in the PR, not CI): `uv run fde-agents-local --help` and `uv run fde-agents-local doctor` (expected: db FAIL if no `FDE_DB_DSN` — that's the check working).
- [ ] **Step 6: Commit:** `git commit -m "feat: fde-agents-local driver -- run any agent's full flow locally, with doctor preflight"`

---

### Task 7: Docs, gates, PR

**Files:**
- Create: `docs/12-providers.md`
- Modify: `README.md` (new collapsible after "Choosing models"), `CLAUDE.md` (one Commands line), `docs/superpowers/specs/2026-08-02-multi-provider-llm-design.md` (status line → implemented)

**Interfaces:** none — documentation and delivery.

- [ ] **Step 1: Write `docs/12-providers.md`** with these sections (concrete, no placeholders):
  - **Provider matrix** — columns: provider · `FDE_MODEL_PROVIDER` · chat ✓ · embeddings (`bedrock` ✓ Titan/Cohere · `anthropic` ✗ pair with another · `openai` ✓ `text-embedding-3-*` @ dimensions=1024 · `gemini` ✓ `gemini-embedding-001` @ output_dimensionality=1024 · `openai-compat` ✓ if the server serves `/v1/embeddings` — 1024-dim models: mxbai-embed-large, bge-m3, snowflake-arctic-embed-l) · credential env var.
  - **Login walkthrough** — `uv run fde-providers login anthropic` → `status`; env vars override for CI/servers; keys never land in files.
  - **Run the full flow locally** — the exact command sequence: compose db + rebuild + `FDE_DB_DSN`, `fde-providers login openai`, `export FDE_MODEL_PROVIDER=anthropic FDE_MODEL_ID=claude-sonnet-5 FDE_EMBED_PROVIDER=openai FDE_EMBED_MODEL_ID=text-embedding-3-small`, `fde-agents-local doctor`, `fde-agents-local engagement --task ingest_interview --engagement-id <id> --input '{"material": "..."}'`, then review at `fde-gate-dev`. State the pairing rule (Anthropic/OpenCode-Zen chat needs a separate embedding provider).
  - **Compat presets** — table: `opencode-zen` → `https://opencode.ai/zen/v1` (get a key at opencode.ai) · `ollama` / `lmstudio` → local; Pi via explicit `FDE_MODEL_BASE_URL` (its endpoint is account-configurable). Keyless local servers work without login (`"local"` placeholder sent).
  - **Caveats, honestly labeled** — provider HTTP paths are stub-tested in CI, live-validated only with real keys (mirror the AWS honesty wording); switching `FDE_EMBED_MODEL_ID`/provider mid-graph strands old vectors (pick before ingesting or re-enqueue); `fde-training`'s judge/trace paths remain Bedrock-only; the 1024-dim constraint is a DB domain, not a preference.
- [ ] **Step 2: README + CLAUDE.md.** README: `<details><summary><b>Bring your own model provider (Anthropic API, OpenAI, Gemini, any /v1)</b></summary>` with the six-line quickstart from docs/12 and a link. **Do not touch** the pinned strings (`0.78`, docs/06 weights, CI smoke-count step name — `test_docs_sync.py`). Add docs/12 to the reading-list table. CLAUDE.md Commands block gains: `uv run fde-providers login <provider>  # then: fde-agents-local <agent> --task ... (docs/12)`.
- [ ] **Step 3: Full gates** (all must pass): `uv run pytest packages` · `uv run ruff check packages && uv run ruff format --check packages` · `uv run mypy` · `uv lock --check`. Run `FDE_DB_DSN=postgresql:///fde uv run pytest packages` too if a local db exists (545+ tests).
- [ ] **Step 4: Commit docs, push, open PR.**

```bash
git add -A && git commit -m "docs: provider matrix, login walkthrough, local full-flow guide"
git push -u origin feat/multi-provider-llm
gh pr create --title "feat: multi-provider LLM support (Anthropic/OpenAI/Gemini/OpenAI-compat) + local driver" --body "..."
```

PR body: summary table of the five providers, the login UX, the local-flow walkthrough, what is stub-tested vs live-validated, and the spec/plan paths. End with the standard attribution line. **Stop after opening the PR — the user reviews and merges** (9 required checks must pass; `deploy` is main-only and will not run on the PR).

---

## Self-review notes (already applied)

- Spec coverage: §1→Tasks 3-4, §2→Tasks 1-2, §3→Task 5, §4→Task 6, §5→Tasks 1-7 (tests inline per task; docs Task 7). Non-goals respected (no training-path changes, no Secrets Manager wiring, no schema changes).
- Type consistency: `ModelBackendSettings` fields match between Task 3 config code, Task 3 tests (`_backend()`), Task 4 runtime usage, and Task 6 doctor. `peek_api_key` tuple shape consistent across Tasks 1/2/6. `validate_api_key(provider, key, *, base_url=None)` consistent across Tasks 2/6.
- Known interplay risks called out where they bite: fde-mcp conftest `_fake_bedrock` (Task 5 Step 1 capture-at-import), fde-agents conftest cache-clearing (Task 4 test), unique test basenames (verified), LRU cache cross-test pollution (distinct texts + provider in cache key).
