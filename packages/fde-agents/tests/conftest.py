"""Shared fixtures for the fde_agents test suite.

Design
------
None of these tests touch a live database, AWS account, or Bedrock model --
the whole package's job is to sit on top of `fde_mcp` (already tested against
a live Postgres in `packages/fde-mcp/tests`) and Strands/AgentCore SDKs
(already tested by their own maintainers). What these tests verify is the
seam: given a fake MCP client and a fake Strands `Agent`, does
`fde_agents.common.runtime` dispatch tasks, frame errors, compute outcomes,
and drive the HITL wait exactly as documented.

`_clear_agent_runtime_settings_cache` mirrors `fde_mcp`'s own
`test_config.py` pattern (see that module's docstring): `get_agent_runtime_
settings()` is `lru_cache`d, so a test that monkeypatches `os.environ` must
clear the cache both before and after, or it either reads stale settings or
leaks its own settings into the next test.
"""

from __future__ import annotations

import os
from collections.abc import Iterator

import pytest

os.environ.setdefault("FDE_AGENT_NAME", "engagement")
os.environ.setdefault("FDE_AGENT_RUNTIME_ARN", "pytest:fde-agents-tests")

from agent_fakes import FakeAsyncTaskApp, FakeMcpClient, make_tool_result

from fde_agents.common.config import get_agent_runtime_settings
from fde_mcp.config import get_settings

__all__ = ["FakeAsyncTaskApp", "FakeMcpClient", "make_tool_result"]


@pytest.fixture(autouse=True)
def _clear_settings_caches() -> Iterator[None]:
    get_settings.cache_clear()
    get_agent_runtime_settings.cache_clear()
    yield
    get_settings.cache_clear()
    get_agent_runtime_settings.cache_clear()
