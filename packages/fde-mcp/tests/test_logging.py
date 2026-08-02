"""Tests for fde_mcp.logging.

No live Postgres or AWS needed -- these exercise the structlog wiring
itself: idempotent configuration, the stdlib root handler it installs, and
the contextvars helpers (`bind_session`/`log_context`/`clear_context`) that
let a tool call tag its log lines without threading fields through every
call signature (see the module docstring for why that matters for
CloudWatch Logs Insights joins).
"""

from __future__ import annotations

import logging

import structlog

from fde_mcp import logging as fde_logging


def test_get_logger_returns_a_working_bound_logger() -> None:
    log = fde_logging.get_logger(__name__)
    # Smoke test: none of these should raise, regardless of configured level.
    log.info("test_event", answer=42)
    log.debug("test_event_debug")
    log.warning("test_event_warning", detail="x")


def test_get_logger_binds_the_module_name() -> None:
    with fde_logging.log_context():
        log = fde_logging.get_logger("fde_mcp.some.module")
        # BoundLogger stores pending bind() calls; the module name should be
        # present in the object's own bound context, not silently dropped.
        bound_fields = log._context
    assert bound_fields.get("logger") == "fde_mcp.some.module"


def test_get_logger_without_a_name_does_not_bind_logger_field() -> None:
    log = fde_logging.get_logger()
    bound_fields = log._context
    assert "logger" not in bound_fields


def test_configure_logging_is_idempotent() -> None:
    fde_logging.configure_logging()
    root = logging.getLogger()
    handlers_after_first = list(root.handlers)

    fde_logging.configure_logging()  # second call must be a no-op
    assert root.handlers == handlers_after_first


def test_bind_session_and_clear_context_round_trip() -> None:
    fde_logging.clear_context()
    try:
        fde_logging.bind_session(
            "sess-123", engagement_id="eng-456", agent="engagement", extra_field="x"
        )
        bound = structlog.contextvars.get_contextvars()
        assert bound["session_id"] == "sess-123"
        assert bound["engagement_id"] == "eng-456"
        assert bound["agent"] == "engagement"
        assert bound["extra_field"] == "x"
    finally:
        fde_logging.clear_context()

    assert structlog.contextvars.get_contextvars() == {}


def test_bind_session_omits_optional_fields_when_not_given() -> None:
    fde_logging.clear_context()
    try:
        fde_logging.bind_session("sess-only")
        bound = structlog.contextvars.get_contextvars()
        assert bound == {"session_id": "sess-only"}
    finally:
        fde_logging.clear_context()


def test_log_context_restores_previous_values_on_exit() -> None:
    fde_logging.clear_context()
    try:
        fde_logging.bind_session("outer-session", agent="engagement")
        with fde_logging.log_context(agent="workflow", tool="kg_search"):
            nested = structlog.contextvars.get_contextvars()
            assert nested["agent"] == "workflow"
            assert nested["tool"] == "kg_search"
        restored = structlog.contextvars.get_contextvars()
        assert restored["agent"] == "engagement"
        assert "tool" not in restored
    finally:
        fde_logging.clear_context()
