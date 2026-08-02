"""config.py -- fde_training's process configuration.

Design
------
Mirrors `fde_mcp.config`'s shape exactly: small frozen dataclasses,
assembled once by `Settings.from_env()`, handed out by an `lru_cache`d
`get_settings()`. Not because uniformity is a virtue in itself, but because
`fde_mcp.config`'s own docstring identifies the actual failure mode a
second, differently-shaped config module would reintroduce: four files each
inventing their own "what's the default and how do I read it" convention,
and no single place to look when a training job connects somewhere
unexpected.

Database settings are NOT redeclared here
-------------------------------------------
`fde_training`'s batch scripts (`export_sft.py`, `rollout_env.py`,
`rival_grader.py`, ...) talk to the SAME Postgres the MCP server does,
addressed by the SAME `FDE_DB_*` environment variables -- see
`fde_mcp.config.DatabaseSettings` for the full DSN-resolution story (`dsn` >
`secret_arn` > `iam_auth`). Re-parsing those variables here, even with
identical defaults, is exactly the kind of duplicate-source-of-truth that
makes "why did the export job connect to a different database than the
agent" a multi-file archaeology exercise instead of a one-line diff. So
`Settings.db` IS an `fde_mcp.config.DatabaseSettings`, imported and reused,
not copied.

`fde_training`'s own DSN RESOLUTION (as opposed to the settings that feed
it) is intentionally a strict subset of `fde_mcp.db`'s: only `db.dsn`, with
a local-peer-auth fallback for laptop/dev use (see `common.resolve_dsn`).
The secret_arn/iam_auth machinery in `fde_mcp.db._resolve_dsn` is async,
tied to a connection-pool `configure` callback, and pulls in `boto3`'s
`asyncio.to_thread` shims -- overkill for the sync, one-shot-per-invocation
scripts in this package, and NOT something a training job strictly needs
(a wrapper script can already export a freshly-minted `FDE_DB_DSN` with an
IAM token before invoking any of these entrypoints; see `common.py`'s
module docstring).

What IS new here
------------------
Two database ROLES that only ever exist in this package's world:
`fde_training` (read-mostly analytics/export access, see
`db/010_roles_and_seed_policy.sql`) and `fde_rl_rollout` (the RL rollout's
narrower, read-the-graph/write-your-own-trace role, see
`db/012_rl_rollout_role.sql`). Neither belongs in `fde_mcp.config` because
`fde_mcp` never runs as either of them.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache

from fde_mcp.config import DatabaseSettings
from fde_mcp.config import get_settings as _get_mcp_settings


def _env_str(name: str, default: str) -> str:
    return os.environ.get(name, default)


@dataclass(frozen=True, slots=True)
class TrainingRoleSettings:
    """The two Postgres roles this package's scripts run as.

    Attributes:
        training_role: `FDE_DB_TRAINING_ROLE`, default "fde_training". The
            role `common.connect()` downgrades to via `SET LOCAL ROLE` for
            every batch read/export (`export_sft.py`, `rival_grader.py`,
            `bedrock_rft.py`). Read-mostly: SELECT on kg.*/trn.*, a narrow
            set of INSERT/UPDATE grants on trn.duel/eval_query/
            retriever_variant/failure_label plus
            `trn.trace_session(split, outcome, label_source)` -- see
            `db/010_roles_and_seed_policy.sql`. Cannot write kg.* or
            hitl.* under any circumstances.
        rollout_role: `FDE_DB_ROLLOUT_ROLE`, default "fde_rl_rollout". The
            role `rollout_env.RolloutEnv` runs every tool call and trace
            write as -- see `db/012_rl_rollout_role.sql`. Strictly narrower
            than `training_role`: it may write its OWN trace rows (so an
            untrusted policy under exploration can record what it did) but
            has no path to hitl.* at all, and (per that migration) cannot
            even UPDATE its own `trn.trace_session` outcome -- it cannot
            manufacture its own training label.
    """

    training_role: str
    rollout_role: str

    @classmethod
    def from_env(cls) -> TrainingRoleSettings:
        return cls(
            training_role=_env_str("FDE_DB_TRAINING_ROLE", "fde_training"),
            rollout_role=_env_str("FDE_DB_ROLLOUT_ROLE", "fde_rl_rollout"),
        )


@dataclass(frozen=True, slots=True)
class Settings:
    """The whole process configuration for fde_training, assembled once by
    `from_env()` and handed out by `get_settings()`.

    Attributes:
        db: See `fde_mcp.config.DatabaseSettings` -- reused wholesale, see
            module docstring for why.
        roles: See `TrainingRoleSettings`.
    """

    db: DatabaseSettings
    roles: TrainingRoleSettings

    @classmethod
    def from_env(cls) -> Settings:
        return cls(db=_get_mcp_settings().db, roles=TrainingRoleSettings.from_env())


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process-wide `Settings`, reading `os.environ` on first
    call and caching thereafter. See `fde_mcp.config.get_settings`'s
    docstring for why this is cached rather than read fresh every call --
    the same "process configuration, not runtime state" argument applies
    here unchanged. Tests that monkeypatch the environment must call
    `get_settings.cache_clear()` (and, because `Settings.db` is sourced
    from `fde_mcp.config`, that module's own `get_settings.cache_clear()`
    too) to observe a fresh read.
    """
    return Settings.from_env()
