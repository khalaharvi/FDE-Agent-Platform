"""Shared fixtures for the fde_training test suite.

Design
------
Almost everything in this package's test suite is pure-function (the
reward terms, `export_sft`'s mask/dedup/split logic, `fde_training.config`)
and needs neither a live Postgres nor AWS credentials -- that is the whole
point of the "no heavy imports, no live dependencies" contract documented
in `fde_training.rewards._episode`'s module docstring. The `requires_db`
marker (registered in the workspace root `pyproject.toml`, shared with
`fde_mcp`/`fde_agents`) exists for the day this package grows a test that
DOES need a live database (e.g. a `rollout_env.RolloutEnv` integration
test); `pytest_collection_modifyitems` below skips those cleanly when
`FDE_DB_DSN` is unset, matching `fde_mcp`'s own conftest, rather than
letting each such test fail loudly with a connection error.
"""

from __future__ import annotations

import os

import pytest


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Skip every `requires_db` test cleanly when no DSN is configured."""
    if os.environ.get("FDE_DB_DSN"):
        return
    skip_db = pytest.mark.skip(reason="FDE_DB_DSN not set; no live Postgres to test against")
    for item in items:
        if "requires_db" in item.keywords:
            item.add_marker(skip_db)
