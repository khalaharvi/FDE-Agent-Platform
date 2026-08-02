"""Tests for actor hashing: HMAC stability, salt resolution, and fail-closed.

The last test in this file is the one that matters most. `sor.observation.
actor_hash` is documented as "Never a name, never an email" (db/007:61), and
the way that promise usually breaks is not a bad hash -- it is the raw
identifier surviving somewhere incidental: a dataclass field, a repr in a
traceback, an exception message, a structured log. So it is asserted
explicitly rather than left to the code reading as if it were true.
"""

from __future__ import annotations

import json
from dataclasses import asdict
from typing import Any

import pytest

from fde_sor import hashing
from fde_sor.adapters.base import AdapterRow
from fde_sor.config import get_settings
from fde_sor.hashing import (
    SaltUnavailableError,
    actor_hash,
    build_hasher,
    resolve_actor_hasher,
    resolve_salt,
)
from fde_sor.mapping import MappingSpec, RecordError, normalize
from fde_sor.observations import ingest

MINIMAL: dict[str, Any] = {
    "case_id_field": "key",
    "activity_field": "status",
    "activity_map": {"In Review": "act.legal_review"},
    "timestamp_field": "updated",
}
WITH_ACTOR = {**MINIMAL, "actor_field": "assignee"}

# The literal that must not appear anywhere except as HMAC input.
RAW_IDENTIFIER = "jane.doe@customer.example.com"


# ===========================================================================
# The primitive
# ===========================================================================
def test_actor_hash_is_stable_for_the_same_salt_and_identifier() -> None:
    assert actor_hash(b"salt", RAW_IDENTIFIER) == actor_hash(b"salt", RAW_IDENTIFIER)


def test_actor_hash_differs_across_salts() -> None:
    """Per-engagement salting is what stops two customers' hashes from being
    comparable; if the salt did not change the output, it would not.
    """
    assert actor_hash(b"engagement-a", RAW_IDENTIFIER) != actor_hash(
        b"engagement-b", RAW_IDENTIFIER
    )


def test_actor_hash_differs_across_identifiers() -> None:
    assert actor_hash(b"salt", "person-a") != actor_hash(b"salt", "person-b")


def test_actor_hash_is_a_full_length_sha256_hex_digest() -> None:
    digest = actor_hash(b"salt", RAW_IDENTIFIER)
    assert len(digest) == 64
    assert all(c in "0123456789abcdef" for c in digest)


def test_build_hasher_matches_the_primitive() -> None:
    assert build_hasher(b"salt")(RAW_IDENTIFIER) == actor_hash(b"salt", RAW_IDENTIFIER)


# ===========================================================================
# Salt resolution and fail-closed
# ===========================================================================
async def test_env_salt_wins_and_is_used(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FDE_ACTOR_HASH_SALT", "local-dev-salt")
    get_settings.cache_clear()
    assert await resolve_salt("eng-1") == b"local-dev-salt"


async def test_no_actor_field_needs_no_salt(monkeypatch: pytest.MonkeyPatch) -> None:
    """A mapping that never reads an identifier must not require a secret --
    otherwise every adapter would need salt provisioning whether or not it
    observes actors.
    """
    monkeypatch.delenv("FDE_ACTOR_HASH_SALT", raising=False)
    get_settings.cache_clear()

    def _explode(_engagement: str) -> bytes | None:  # pragma: no cover - must not run
        raise AssertionError("salt resolution must not be attempted without an actor_field")

    monkeypatch.setattr(hashing, "resolve_salt", _explode)
    assert await resolve_actor_hasher(MappingSpec.parse(MINIMAL), "eng-1") is None


async def test_actor_field_without_a_resolvable_salt_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("FDE_ACTOR_HASH_SALT", raising=False)
    get_settings.cache_clear()
    hashing.clear_salt_cache()

    async def _no_secret(secret_name: str) -> bytes | None:
        return None

    monkeypatch.setattr(hashing, "_fetch_salt_from_secrets_manager", _no_secret)

    with pytest.raises(SaltUnavailableError) as exc_info:
        await resolve_actor_hasher(MappingSpec.parse(WITH_ACTOR), "eng-1")

    message = str(exc_info.value)
    assert "FDE_ACTOR_HASH_SALT" in message, "the error must say how to fix it"
    assert "fde/actor-hash-salt/eng-1" in message, "and which secret is missing"


async def test_ingest_refuses_before_any_fetch_when_the_salt_is_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fail-closed means fail EARLY: not one call to the customer's system, let
    alone one row written, when the salt cannot be resolved.
    """
    monkeypatch.delenv("FDE_ACTOR_HASH_SALT", raising=False)
    get_settings.cache_clear()
    hashing.clear_salt_cache()

    async def _no_secret(secret_name: str) -> bytes | None:
        return None

    monkeypatch.setattr(hashing, "_fetch_salt_from_secrets_manager", _no_secret)

    fetched = False

    class _TripwireAdapter:
        kind = "replay"

        async def fetch(self, cursor: str | None) -> Any:
            nonlocal fetched
            fetched = True
            yield  # pragma: no cover - reaching here is the failure

    row = AdapterRow(
        adapter_id=1,
        engagement_id="eng-1",
        adapter_key="jira-prod",
        system_node_key="sys.jira",
        kind="rest_poll",
        secret_arn=None,
        mapping=WITH_ACTOR,
        last_cursor=None,
    )
    with pytest.raises(SaltUnavailableError):
        await ingest(_TripwireAdapter(), row)
    assert fetched is False, "the adapter must never be asked for records"


async def test_secrets_manager_salt_is_cached_per_engagement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("FDE_ACTOR_HASH_SALT", raising=False)
    get_settings.cache_clear()
    hashing.clear_salt_cache()
    calls: list[str] = []

    async def _fetch(secret_name: str) -> bytes | None:
        calls.append(secret_name)
        return b"secret-salt"

    monkeypatch.setattr(hashing, "_fetch_salt_from_secrets_manager", _fetch)

    assert await resolve_salt("eng-1") == b"secret-salt"
    assert await resolve_salt("eng-1") == b"secret-salt"
    assert calls == ["fde/actor-hash-salt/eng-1"], "one fetch per engagement per process"

    await resolve_salt("eng-2")
    assert calls[-1] == "fde/actor-hash-salt/eng-2", "a different engagement is a different salt"


# ===========================================================================
# PII: the raw identifier must not survive normalisation
# ===========================================================================
def test_raw_identifier_never_appears_on_the_normalized_observation() -> None:
    observation = normalize(
        MappingSpec.parse(WITH_ACTOR),
        {
            "key": "CPQ-1",
            "status": "In Review",
            "updated": "2026-03-01T10:00:00Z",
            "assignee": RAW_IDENTIFIER,
        },
        actor_hasher=build_hasher(b"salt"),
    )

    assert observation.actor_hash == actor_hash(b"salt", RAW_IDENTIFIER)

    # Every representation something downstream might serialise, log, or paste
    # into a bug report.
    surfaces = [
        repr(observation),
        str(observation),
        json.dumps(asdict(observation), default=str),
    ]
    for surface in surfaces:
        assert RAW_IDENTIFIER not in surface, (
            "the raw actor identifier reached a representation of the "
            "observation; it must exist only as HMAC input"
        )
        assert "jane.doe" not in surface
        assert "customer.example.com" not in surface


def test_record_errors_name_fields_not_values() -> None:
    """A malformed record's error message is collected into `IngestStats` and
    logged. It must identify the FIELD so the mapping can be fixed, and carry
    none of the record's content.
    """
    with pytest.raises(RecordError) as exc_info:
        normalize(
            MappingSpec.parse(WITH_ACTOR),
            {
                "key": "CPQ-1",
                "status": "In Review",
                "updated": "not-a-timestamp",
                "assignee": RAW_IDENTIFIER,
            },
            actor_hasher=build_hasher(b"salt"),
        )
    message = str(exc_info.value)
    assert "updated" in message, "the failing field must be named"
    assert RAW_IDENTIFIER not in message
    assert "not-a-timestamp" not in message, "the value itself is not needed to fix a mapping"


def test_salt_bytes_do_not_appear_in_a_hasher_repr() -> None:
    """A bound hasher ends up in an `ingest` call frame; its repr must not leak
    the salt into a traceback.
    """
    hasher = build_hasher(b"super-secret-salt-value")
    assert "super-secret-salt-value" not in repr(hasher)
