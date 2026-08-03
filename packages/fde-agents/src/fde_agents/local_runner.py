"""fde-agents-local -- run any AgentCore agent's full task flow locally and
stream its events to stdout, with no live AgentCore Runtime deployment
needed. Talks to the same MCP tool surface the deployed runtimes use
(stdio by default -- see `fde_agents.common.mcp_tools`), so a task run here
exercises the identical code path a production invocation does.

Usage:
  fde-agents-local <agent> --task <name> --engagement-id <id>
                    [--input '<json>'] [--json]
  fde-agents-local doctor
  fde-agents-local --help

Agents: engagement, workflow, development.

Engagement task names (fde_agents.engagement.agent.VALID_TASKS):
  map_process, score_opportunities, detect_bottlenecks, ingest_interview

Flags:
  --task <name>           required -- the task name for the chosen agent
  --engagement-id <id>     required -- the engagement this task runs against
  --input '<json>'         optional task_input payload (a JSON object),
                           default '{}'
  --json                   emit one JSON object per line instead of the
                           human-readable "[type] ..." format

doctor
  Preflight checks for a local run, each isolated so one broken check does
  not hide the rest: db connectivity (FDE_DB_DSN + SELECT 1), the
  configured model provider's credential plus a live validation call,
  FDE_MODEL_ID resolution, the embeddings provider's configuration, and a
  warning if FDE_GATEWAY_URL is set (local mode expects stdio MCP, not the
  Gateway). Prints "ok <name>" or "FAIL <name>: <problem>" per check;
  exits 1 if any check failed.

Environment prerequisites:
  FDE_DB_DSN                          Postgres DSN, fde_agent role
  FDE_MODEL_PROVIDER + FDE_MODEL_ID    non-bedrock providers must set both;
                                       bedrock (the default) uses the
                                       ambient AWS credential chain plus
                                       MODEL_PRESETS
  FDE_EMBED_PROVIDER                  bedrock (default), openai, gemini,
                                       or openai-compat (needs
                                       FDE_EMBED_BASE_URL)

Examples:
  fde-agents-local engagement --task map_process --engagement-id e-1 \\
      --input '{"process_key": "quote_to_cash"}'
  fde-agents-local doctor
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from typing import Any, cast

from fde_agents.common.config import ModelBackendSettings, resolve_model_id
from fde_agents.common.providers import resolve_base_url
from fde_agents.common.runtime import Handler, StreamEvent, TaskPayload
from fde_mcp.credentials import CredentialError, peek_api_key
from fde_mcp.providers_cli import validate_api_key

_USAGE = __doc__ or ""

_AGENTS = ("engagement", "workflow", "development")

# Any of these set means an AWS credential chain is plausibly configured --
# mirrors fde_mcp.providers_cli's own check for the same reason (bedrock
# authenticates through the ambient chain, not an API key doctor can peek).
_BEDROCK_ENV_VARS = ("AWS_ACCESS_KEY_ID", "AWS_PROFILE", "AWS_ROLE_ARN")

# Kept in sync with fde_mcp.embeddings' own dispatch (embeddings.py's
# embed_batch): the four providers it actually knows how to call.
_VALID_EMBED_PROVIDERS = frozenset({"bedrock", "openai", "openai-compat", "gemini"})

_GATEWAY_WARNING = "local mode expects stdio MCP; unset FDE_GATEWAY_URL"

_FLAG_VALUES: dict[str, str] = {
    "--task": "task",
    "--engagement-id": "engagement_id",
    "--input": "input",
}


class _UsageError(Exception):
    """A malformed invocation. Caught by `main()` and turned into a usage
    message on stderr plus exit code 2.
    """


def _load_handler(agent: str) -> Handler:
    import importlib  # noqa: PLC0415

    module = importlib.import_module(f"fde_agents.{agent}.agent")
    return cast(Handler, module.handler)


def _parse_flags(rest: list[str]) -> tuple[dict[str, str], bool]:
    """Manual argv parsing for the flag pairs following the agent name.
    Returns `(values, json_flag)`; raises `_UsageError` on an unknown flag
    or a value-flag with no value following it.
    """
    values: dict[str, str] = {}
    as_json = False
    i = 0
    while i < len(rest):
        arg = rest[i]
        if arg == "--json":
            as_json = True
            i += 1
            continue
        key = _FLAG_VALUES.get(arg)
        if key is None:
            msg = f"unknown flag {arg!r}"
            raise _UsageError(msg)
        if i + 1 >= len(rest):
            msg = f"{arg} requires a value"
            raise _UsageError(msg)
        values[key] = rest[i + 1]
        i += 2
    return values, as_json


def _parse_run_args(rest: list[str]) -> tuple[str, str, dict[str, Any], bool]:
    """`(task, engagement_id, task_input, as_json)`, or raises `_UsageError`
    for anything main() should report as a usage problem -- missing
    required flags, malformed --input JSON, or a non-object --input.
    """
    values, as_json = _parse_flags(rest)
    task = values.get("task")
    engagement_id = values.get("engagement_id")
    if not task or not engagement_id:
        msg = "--task and --engagement-id are required"
        raise _UsageError(msg)
    try:
        task_input = json.loads(values.get("input", "{}"))
    except json.JSONDecodeError as exc:
        msg = f"--input is not valid JSON: {exc}"
        raise _UsageError(msg) from exc
    if not isinstance(task_input, dict):
        msg = "--input must be a JSON object"
        raise _UsageError(msg)
    return task, engagement_id, task_input, as_json


def _format_human(event: StreamEvent) -> str:
    kind = event.get("type", "event")
    if kind == "status":
        return f"[status] {event.get('message', '')}"
    if kind == "error":
        return f"[error] {event.get('error') or event.get('message', '')}"
    if kind == "done":
        return f"[done] outcome={event.get('outcome')}"
    if kind == "delta":
        return str(event.get("text", ""))
    fields = " ".join(f"{k}={v}" for k, v in event.items() if k != "type")
    return f"[{kind}] {fields}"


async def _drive(handler: Handler, payload: TaskPayload, *, as_json: bool) -> int:
    saw_error = False
    async for event in handler(payload, None):
        if event.get("type") == "error":
            saw_error = True
        line = json.dumps(event) if as_json else _format_human(event)
        sys.stdout.write(line + "\n")
    return 1 if saw_error else 0


def run(
    agent: str,
    task: str,
    engagement_id: str,
    task_input: dict[str, Any],
    *,
    as_json: bool,
) -> int:
    handler = _load_handler(agent)
    if os.environ.get("FDE_GATEWAY_URL"):
        sys.stderr.write(f"fde-agents-local: {_GATEWAY_WARNING}\n")
    payload: TaskPayload = {"task": task, "engagement_id": engagement_id, "input": task_input}
    return asyncio.run(_drive(handler, payload, as_json=as_json))


# ---------------------------------------------------------------------------
# doctor
# ---------------------------------------------------------------------------
def _check_db() -> tuple[str, str | None]:
    dsn = os.environ.get("FDE_DB_DSN")
    if not dsn:
        return ("db", "FDE_DB_DSN is not set")
    import psycopg  # noqa: PLC0415 -- only the db check needs a connection

    try:
        with psycopg.connect(dsn, connect_timeout=5) as conn, conn.cursor() as cur:
            cur.execute("SELECT 1")
    except Exception as exc:  # infra failure -- report, doctor must not crash
        return ("db", f"{type(exc).__name__}: {exc}")
    return ("db", None)


def _check_model_provider() -> tuple[str, str | None]:
    backend = ModelBackendSettings.from_env()
    provider = backend.provider
    if provider == "bedrock":
        if any(os.environ.get(var) for var in _BEDROCK_ENV_VARS):
            return ("model provider", None)
        return (
            "model provider",
            "no AWS credential chain detected (set AWS_ACCESS_KEY_ID / AWS_PROFILE / "
            "AWS_ROLE_ARN, or rely on an instance/task role)",
        )
    try:
        key, source = peek_api_key(provider)
        if key is None:
            return ("model provider", f"no key: run fde-providers login {provider}")
        base_url = resolve_base_url(backend) if provider == "openai-compat" else None
        error = validate_api_key(provider, key, base_url=base_url)
    except (CredentialError, ValueError) as exc:
        # Unknown FDE_MODEL_PROVIDER value (peek_api_key) or an openai-compat
        # backend missing both FDE_MODEL_BASE_URL and FDE_MODEL_COMPAT_PRESET
        # (resolve_base_url) -- doctor reports it as a failed check, not a crash.
        return ("model provider", str(exc))
    if error is not None:
        return ("model provider", f"{error} (key from {source})")
    return ("model provider", None)


def _check_model_id() -> tuple[str, str | None]:
    try:
        resolve_model_id("engagement")
    except ValueError as exc:
        return ("model id", str(exc))
    return ("model id", None)


def _check_embeddings() -> tuple[str, str | None]:
    provider = os.environ.get("FDE_EMBED_PROVIDER", "bedrock")
    if provider not in _VALID_EMBED_PROVIDERS:
        return (
            "embeddings",
            f"FDE_EMBED_PROVIDER={provider!r} invalid; choose one of "
            f"{sorted(_VALID_EMBED_PROVIDERS)}",
        )
    if provider == "openai-compat" and not os.environ.get("FDE_EMBED_BASE_URL"):
        return ("embeddings", "FDE_EMBED_PROVIDER=openai-compat requires FDE_EMBED_BASE_URL")
    if provider not in ("bedrock", "openai-compat"):
        key, _source = peek_api_key(provider)
        if key is None:
            return ("embeddings", f"no key: run fde-providers login {provider}")
    return ("embeddings", None)


def _check_gateway() -> tuple[str, str | None]:
    if os.environ.get("FDE_GATEWAY_URL"):
        return ("gateway", _GATEWAY_WARNING)
    return ("gateway", None)


def _doctor_checks() -> list[tuple[str, str | None]]:
    return [
        _check_db(),
        _check_model_provider(),
        _check_model_id(),
        _check_embeddings(),
        _check_gateway(),
    ]


def _cmd_doctor() -> int:
    failed = False
    for name, problem in _doctor_checks():
        if problem is None:
            sys.stdout.write(f"ok {name}\n")
        else:
            sys.stdout.write(f"FAIL {name}: {problem}\n")
            failed = True
    return 1 if failed else 0


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv

    if not args or args[0] in ("-h", "--help"):
        sys.stdout.write(_USAGE)
        return 0

    command, *rest = args

    if command == "doctor":
        return _cmd_doctor()

    if command not in _AGENTS:
        sys.stderr.write(
            f"fde-agents-local: unknown agent {command!r}; expected one of "
            f"{(*_AGENTS, 'doctor')}. Run `fde-agents-local --help`.\n"
        )
        return 2

    try:
        task, engagement_id, task_input, as_json = _parse_run_args(rest)
    except _UsageError as exc:
        sys.stderr.write(f"fde-agents-local: {exc}. Run `fde-agents-local --help`.\n")
        return 2

    return run(command, task, engagement_id, task_input, as_json=as_json)


if __name__ == "__main__":
    sys.exit(main())
