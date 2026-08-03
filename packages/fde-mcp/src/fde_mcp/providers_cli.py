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

# Any of these set means an AWS credential chain is plausibly configured.
_BEDROCK_ENV_VARS = ("AWS_ACCESS_KEY_ID", "AWS_PROFILE", "AWS_ROLE_ARN")


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
    except Exception as exc:  # report, don't crash a CLI
        return f"validation request failed: {exc}"
    if 200 <= status < 300:
        return None
    return f"provider returned HTTP {status} (bad key?)"


def _cmd_login(provider: str) -> int:
    key = _prompt_secret(f"API key for {provider}: ")
    error = validate_api_key(provider, key)
    if error is not None:
        sys.stderr.write(f"fde-providers: {error}\n")
        return 1
    store_api_key(provider, key)
    sys.stdout.write(f"stored key for {provider!r} in the OS keychain (service 'fde-platform')\n")
    return 0


def _cmd_logout(provider: str) -> int:
    removed = delete_api_key(provider)
    if removed:
        sys.stdout.write(f"removed stored key for {provider!r}\n")
    else:
        sys.stdout.write(f"no stored key for {provider!r}\n")
    return 0


def _cmd_status(*, validate: bool) -> int:
    providers = [*sorted(PROVIDER_ENV_VARS), "bedrock"]
    for provider in providers:
        if provider == "bedrock":
            has_aws = any(os.environ.get(var) for var in _BEDROCK_ENV_VARS)
            source = "env" if has_aws else "none (uses AWS credential chain)"
            sys.stdout.write(f"{provider:<15} {source}\n")
            continue

        key, source = peek_api_key(provider)
        line = f"{provider:<15} {source}"
        if validate and key is not None:
            error = validate_api_key(provider, key)
            line += "  ok" if error is None else f"  INVALID: {error}"
        sys.stdout.write(f"{line}\n")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv

    if not args or args[0] in ("-h", "--help"):
        sys.stdout.write(_USAGE)
        return 0

    subcommand, *rest = args

    if subcommand == "login" and len(rest) == 1:
        return _cmd_login(rest[0])
    if subcommand == "logout" and len(rest) == 1:
        return _cmd_logout(rest[0])
    if subcommand == "status":
        return _cmd_status(validate="--no-validate" not in rest)

    if subcommand in ("login", "logout"):
        sys.stderr.write(f"usage: fde-providers {subcommand} <provider>\n")
    else:
        sys.stderr.write(
            f"fde-providers: unknown subcommand {subcommand!r}. Run `fde-providers --help`.\n"
        )
    return 2


if __name__ == "__main__":
    sys.exit(main())
