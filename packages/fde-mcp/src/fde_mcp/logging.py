"""Structured logging for the whole platform.

Design
------
Everything here runs in AWS and every log line ends up in CloudWatch Logs.
CloudWatch Logs Insights can query JSON fields natively but has to
regex-parse plain text, so the default renderer is JSON and human-readable
console output is opt-in (``FDE_LOG_CONSOLE=1``) for local development.

The `session_id` binding is the load-bearing part. AgentCore's
`runtimeSessionId`, the OTEL trace's session id, and `trn.trace_session.
session_id` are deliberately the same value across the platform. Binding it
into structlog's contextvars means every log line emitted anywhere under that
session carries it without being threaded through call signatures -- so a
Logs Insights query can join agent logs, MCP tool logs, and the training
trace table on one field:

    fields @timestamp, event, tool, latency_ms
    | filter session_id = '<id>'
    | sort @timestamp asc

Contextvars rather than a logger-per-request object because the MCP tool
handlers are `async def` called from FastMCP's dispatch, and there is no
convenient seam to pass a bound logger through. `bind_session()` is called
once at the top of a request and every downstream `get_logger()` inherits it,
including inside `asyncio.gather` children.

Why not stdlib `logging.config.dictConfig` alone: the value here is
`event_dict` merging (a tool handler adds `tool=`, the DB layer adds
`statement_timeout=`, both land in one JSON object) which stdlib gives you
only by hand-rolling a Filter.
"""

from __future__ import annotations

import logging
import os
import sys
from typing import TYPE_CHECKING, Any

import structlog

if TYPE_CHECKING:
    from contextlib import AbstractContextManager

__all__ = [
    "bind_session",
    "clear_context",
    "configure_logging",
    "get_logger",
    "log_context",
]

_CONFIGURED = False


def _drop_color_message(
    _logger: structlog.typing.WrappedLogger, _method: str, event_dict: structlog.typing.EventDict
) -> structlog.typing.EventDict:
    """Remove uvicorn/structlog's duplicated `color_message` key.

    Third-party libraries that log through structlog's stdlib bridge attach a
    pre-coloured copy of the message. In JSON output it is a byte-for-byte
    duplicate of `event` and roughly doubles the size of high-volume lines.

    Typed against `structlog.typing.{WrappedLogger,EventDict}` (not
    `object`/`dict[str, Any]`) so this satisfies `structlog.typing.Processor`
    exactly -- a narrower parameter type makes this incompatible with the
    processor chain's contravariant call signature and mypy strict rejects it.
    """
    event_dict.pop("color_message", None)
    return event_dict


def configure_logging(
    *,
    level: str | None = None,
    console: bool | None = None,
    service: str | None = None,
) -> None:
    """Configure structlog + stdlib logging once per process.

    Idempotent: safe to call from every entrypoint (the MCP server, each
    agent, the embedder worker, the training CLIs) without worrying about
    which one ran first.

    Args:
        level: Log level name. Defaults to ``$FDE_LOG_LEVEL`` or ``INFO``.
        console: Human-readable coloured output instead of JSON. Defaults to
            ``$FDE_LOG_CONSOLE`` being truthy. Never enable in a deployed
            runtime -- CloudWatch cannot query it.
        service: Value bound as ``service`` on every line. Defaults to
            ``$FDE_SERVICE_NAME``. Set it: it is what lets one log group hold
            several components without ambiguity.
    """
    global _CONFIGURED  # noqa: PLW0603 -- process-wide config is inherently global
    if _CONFIGURED:
        return

    resolved_level = (level or os.getenv("FDE_LOG_LEVEL") or "INFO").upper()
    use_console = console if console is not None else _env_flag("FDE_LOG_CONSOLE")
    resolved_service = service or os.getenv("FDE_SERVICE_NAME") or "fde"

    numeric_level = logging.getLevelNamesMapping()[resolved_level]

    # Processors shared by BOTH structlog-native calls and stdlib records.
    # `add_logger_name` is deliberately absent: it reads `logger.name`, which
    # only exists on stdlib loggers, and structlog's own BoundLogger has no
    # such attribute. The logger name is bound explicitly in `get_logger()`
    # instead, so the `logger` field is present either way.
    shared: list[structlog.typing.Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        # UTC and ISO-8601. CloudWatch's own timestamp is ingest time, which
        # drifts from emit time under backpressure; this one is emit time.
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.UnicodeDecoder(),
        _drop_color_message,
    ]

    renderer: structlog.typing.Processor
    if use_console:
        renderer = structlog.dev.ConsoleRenderer(colors=sys.stderr.isatty())
    else:
        renderer = structlog.processors.JSONRenderer(sort_keys=True)

    # ProcessorFormatter is what makes stdlib records (boto3, psycopg,
    # uvicorn, the AgentCore SDK) render through the SAME processor chain as
    # structlog calls. Without it a deployed process emits two interleaved
    # formats -- JSON from our code, printf from every dependency -- and the
    # CloudWatch Insights queries only see half the story.
    structlog.configure(
        processors=[
            *shared,
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(numeric_level),
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )

    formatter = structlog.stdlib.ProcessorFormatter(
        # Applied only to records that came from stdlib, not from structlog.
        foreign_pre_chain=[
            *shared,
            structlog.stdlib.ExtraAdder(),
        ],
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            # Must run before the renderer, or tracebacks reach JSONRenderer
            # as an unserialisable ExcInfo tuple and are silently dropped.
            structlog.processors.format_exc_info,
            renderer,
        ],
    )

    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(formatter)
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(numeric_level)

    for noisy in ("botocore", "boto3", "urllib3", "s3transfer"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    structlog.contextvars.bind_contextvars(service=resolved_service)
    _CONFIGURED = True


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    """Return a bound logger, configuring logging on first use.

    Auto-configuring here means a library module can `get_logger(__name__)` at
    import time without every entrypoint remembering to call
    `configure_logging()` first -- a missed call would otherwise produce
    unstructured output in exactly the deployed paths that need JSON.
    """
    if not _CONFIGURED:
        configure_logging()
    logger = structlog.get_logger(name)
    # Bound explicitly rather than via structlog.stdlib.add_logger_name, which
    # cannot see a name on a structlog-native BoundLogger. See configure_logging.
    return logger.bind(logger=name) if name else logger  # type: ignore[no-any-return]


def bind_session(
    session_id: str,
    *,
    engagement_id: str | None = None,
    agent: str | None = None,
    **extra: Any,
) -> None:
    """Bind session-scoped fields onto every subsequent log line in this task.

    Call once at the top of a request or agent invocation. `session_id` must
    be the AgentCore `runtimeSessionId` so log lines join to
    `trn.trace_session` and to CloudWatch GenAI Observability spans on one
    key.
    """
    fields: dict[str, Any] = {"session_id": session_id, **extra}
    if engagement_id is not None:
        fields["engagement_id"] = engagement_id
    if agent is not None:
        fields["agent"] = agent
    structlog.contextvars.bind_contextvars(**fields)


def clear_context() -> None:
    """Drop all bound contextvars. Call when a session ends."""
    structlog.contextvars.clear_contextvars()


def log_context(**fields: Any) -> AbstractContextManager[None]:
    """Temporarily bind fields, restoring the previous values on exit.

    Useful for a nested unit of work that should tag its own lines without
    leaking the tag to its caller::

        with log_context(tool="kg_search", k=20):
            ...
    """
    return structlog.contextvars.bound_contextvars(**fields)


def _env_flag(name: str) -> bool:
    return (os.getenv(name) or "").strip().lower() in {"1", "true", "yes", "on"}
