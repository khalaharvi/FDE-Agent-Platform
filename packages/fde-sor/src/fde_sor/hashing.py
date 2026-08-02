"""hashing.py -- salted, one-way actor identifiers.

`sor.observation.actor_hash` is "opaque, salted hash. Never a name, never an
email" (db/007:61). This module is the only place that hash is produced, and
it is built to fail closed in the one way that matters: if a mapping declares
an `actor_field` but no salt can be resolved, the run stops BEFORE the first
record is fetched. The alternative failure modes are both unacceptable -- an
unsalted hash is a rainbow-table lookup away from the identifier, and
"silently skip hashing and store NULL" would quietly disable actor-drift
detection while every run reported success.

Salt scope: per engagement
---------------------------
The salt lives in Secrets Manager under `{prefix}{engagement_id}`. Per
engagement rather than per platform because the whole point of `actor_hash` is
that two observations by the same person are recognisable as the same person
*within one engagement*; a platform-wide salt would additionally make them
correlatable ACROSS customers, which nothing in the design needs and no
customer agreed to.

`FDE_ACTOR_HASH_SALT` overrides it for local development and CI. That
override is a real risk if it leaks into a deployment -- one shared salt makes
every engagement's hashes comparable -- so it is documented as never-in-prod
in `config.SorSettings` and the resolution path logs which source won.

docs/08 §3 is honest that this is "a tripwire, not a guarantee": the hash is
only as private as the field it is salted from. That argument is about
onboarding review of what adapters populate `actor_field` with, and it does
not change what this module must do.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
from typing import TYPE_CHECKING

from fde_mcp.logging import get_logger
from fde_sor.config import aws_region, get_settings

if TYPE_CHECKING:
    from collections.abc import Callable

    from fde_sor.mapping import MappingSpec

__all__ = [
    "SaltUnavailableError",
    "actor_hash",
    "build_hasher",
    "clear_salt_cache",
    "resolve_actor_hasher",
    "resolve_salt",
]

log = get_logger(__name__)

# Per-process, per-engagement. A Lambda invocation handles one adapter and a
# CronJob handles one poll, so this is one Secrets Manager call per container
# lifetime rather than one per record.
_SALT_CACHE: dict[str, bytes] = {}
_SALT_LOCK = asyncio.Lock()


class SaltUnavailableError(RuntimeError):
    """No actor-hash salt could be resolved for an engagement that needs one.

    Deliberately fatal. See the module docstring: the two ways to continue
    (store the raw identifier, or store NULL and carry on) are both worse than
    stopping.
    """


def actor_hash(salt: bytes, raw_identifier: str) -> str:
    """HMAC-SHA256 of `raw_identifier` under `salt`, hex encoded.

    HMAC rather than `sha256(salt + identifier)` because the naive
    concatenation is length-extendable and, more practically, because HMAC is
    the construction a reviewer can recognise as correct at a glance.
    """
    return hmac.new(salt, raw_identifier.encode("utf-8"), hashlib.sha256).hexdigest()


def build_hasher(salt: bytes) -> Callable[[str], str]:
    """Bind `salt` into a one-argument hasher.

    `mapping.normalize` takes this rather than the salt itself so that the
    salt bytes never enter the normalisation call frame -- one fewer place for
    them to end up in a traceback.
    """

    def _hash(raw_identifier: str) -> str:
        return actor_hash(salt, raw_identifier)

    return _hash


async def _fetch_salt_from_secrets_manager(secret_name: str) -> bytes | None:
    import boto3  # noqa: PLC0415 -- keep boto3 out of the import path for DSN-only local runs

    def _fetch() -> str | None:
        client = boto3.client("secretsmanager", region_name=aws_region())
        try:
            response = client.get_secret_value(SecretId=secret_name)
        except Exception:
            # Not found, no permission, no network: all of them mean "no salt",
            # and the caller turns that into SaltUnavailableError. Logged at
            # warning with the secret NAME (never its value).
            log.warning("actor_salt_fetch_failed", secret_name=secret_name, exc_info=True)
            return None
        secret = response.get("SecretString")
        return str(secret) if secret else None

    raw = await asyncio.to_thread(_fetch)
    return raw.encode("utf-8") if raw else None


async def resolve_salt(engagement_id: str) -> bytes | None:
    """Resolve the salt for `engagement_id`, or `None` if there is none.

    Order: `FDE_ACTOR_HASH_SALT` (local/CI), then Secrets Manager. Returning
    `None` rather than raising keeps this usable for the "does this engagement
    have a salt?" question; `resolve_actor_hasher` is the one that fails
    closed.
    """
    settings = get_settings()
    if settings.actor_hash_salt:
        log.info("actor_salt_resolved", engagement_id=engagement_id, source="env")
        return settings.actor_hash_salt.encode("utf-8")

    async with _SALT_LOCK:
        cached = _SALT_CACHE.get(engagement_id)
        if cached is not None:
            return cached
        secret_name = f"{settings.actor_salt_secret_prefix}{engagement_id}"
        salt = await _fetch_salt_from_secrets_manager(secret_name)
        if salt is None:
            return None
        _SALT_CACHE[engagement_id] = salt
        log.info(
            "actor_salt_resolved",
            engagement_id=engagement_id,
            source="secrets_manager",
            secret_name=secret_name,
        )
        return salt


def clear_salt_cache() -> None:
    """Drop the per-process salt cache. For tests and for a CLI that switches
    engagements inside one process.
    """
    _SALT_CACHE.clear()


async def resolve_actor_hasher(
    spec: MappingSpec, engagement_id: str
) -> Callable[[str], str] | None:
    """The fail-closed entry point. Call this BEFORE fetching any record.

    * mapping has no `actor_field` -> no identifier is ever read, so no salt is
      needed and `None` is returned. `actor_hash`/`actor_role_key` stay NULL,
      and the actor-drift detector simply has nothing to say about this
      adapter, which is the documented behaviour (docs/08 §2.1).
    * mapping has an `actor_field` and a salt resolves -> a bound hasher.
    * mapping has an `actor_field` and no salt resolves -> `SaltUnavailableError`,
      raised here rather than at the first record, so a misconfigured adapter
      cannot make a single SoR API call, let alone write a row.
    """
    if spec.actor_field is None:
        return None
    salt = await resolve_salt(engagement_id)
    if salt is None:
        settings = get_settings()
        msg = (
            f"mapping declares actor_field {spec.actor_field!r} but no actor-hash "
            f"salt is available for engagement {engagement_id}: set "
            f"FDE_ACTOR_HASH_SALT (local/CI only) or create the Secrets Manager "
            f"secret {settings.actor_salt_secret_prefix}{engagement_id}. Refusing "
            "to ingest, because the alternatives are storing a raw identifier or "
            "silently disabling actor-drift detection."
        )
        raise SaltUnavailableError(msg)
    return build_hasher(salt)
