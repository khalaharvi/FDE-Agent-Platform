"""Tests for the mapping contract: path resolution, normalisation, dedup keys.

No database, no network, no AWS. `mapping.py` is deliberately pure so that
every worked example in docs/08 -- the whole surface a human authors at
onboarding -- can be checked here, where a failure names the field rather than
appearing three layers down as a NOT NULL violation.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from typing import Any

import pytest

from fde_sor.mapping import (
    MappingError,
    MappingSpec,
    RecordError,
    compute_dedup_key,
    normalize,
    resolve_path,
)

MINIMAL: dict[str, Any] = {
    "case_id_field": "key",
    "activity_field": "status",
    "activity_map": {"In Review": "act.legal_review"},
    "timestamp_field": "updated",
}


def _spec(**overrides: Any) -> MappingSpec:
    return MappingSpec.parse({**MINIMAL, **overrides})


# ===========================================================================
# resolve_path
# ===========================================================================
def test_resolve_path_walks_nested_dicts() -> None:
    payload = {"fields": {"assignee": {"accountId": "acct-1"}}}
    assert resolve_path(payload, "fields.assignee.accountId") == "acct-1"


def test_resolve_path_indexes_lists_with_integer_segments() -> None:
    payload = {"changelog": [{"field": "status"}, {"field": "assignee"}]}
    assert resolve_path(payload, "changelog.1.field") == "assignee"


def test_resolve_path_returns_none_for_every_kind_of_absence() -> None:
    payload = {"fields": {"status": "Open"}, "items": [1]}
    assert resolve_path(payload, "fields.missing") is None
    assert resolve_path(payload, "missing.deeper") is None
    assert resolve_path(payload, "items.7") is None, "out-of-range index"
    assert resolve_path(payload, "items.notanindex") is None
    assert resolve_path(payload, "fields.status.name") is None, "cannot index into a scalar"


# ===========================================================================
# MappingSpec.parse
# ===========================================================================
def test_parse_accepts_the_docs_jira_example() -> None:
    spec = MappingSpec.parse(
        {
            "activity_field": "status",
            "activity_map": {"In Review": "act.legal_review"},
            "actor_field": "assignee.accountId",
            "actor_role_map": {"5b10a2844c20165700ede21g": "role.deal_desk"},
            "timestamp_field": "updated",
            "case_id_field": "key",
            "duration_field": None,
        }
    )
    assert spec.case_id_field == "key"
    assert spec.actor_role_map["5b10a2844c20165700ede21g"] == "role.deal_desk"
    assert spec.duration_unit == "s"


def test_parse_names_every_missing_required_key_at_once() -> None:
    with pytest.raises(MappingError) as exc_info:
        MappingSpec.parse({})
    message = str(exc_info.value)
    for key in ("case_id_field", "activity_field", "timestamp_field", "activity_map"):
        assert key in message, f"{key} must be named so one edit fixes the mapping"


def test_parse_rejects_an_unknown_duration_unit() -> None:
    with pytest.raises(MappingError, match="duration_unit"):
        MappingSpec.parse({**MINIMAL, "duration_field": "d", "duration_unit": "minutes"})


def test_parse_rejects_actor_role_map_without_actor_field() -> None:
    """An actor_role_map with no actor_field silently produces NULL for every
    actor_role_key -- i.e. actor drift detection that looks configured and
    reports nothing.
    """
    with pytest.raises(MappingError, match="actor_field"):
        MappingSpec.parse({**MINIMAL, "actor_role_map": {"a": "role.x"}})


def test_parse_keeps_namespaced_kind_blocks() -> None:
    spec = MappingSpec.parse(
        {
            **MINIMAL,
            "request": {"url": "https://example.invalid/api"},
            "stream": {"queue_url": "https://sqs.invalid/q", "envelope": "sns"},
            "cdc": {"slot_name": "fde_slot"},
        }
    )
    assert spec.request["url"] == "https://example.invalid/api"
    assert spec.stream["envelope"] == "sns"
    assert spec.cdc["slot_name"] == "fde_slot"


# ===========================================================================
# normalize
# ===========================================================================
def test_normalize_maps_a_known_activity() -> None:
    observation = normalize(
        _spec(), {"key": "CPQ-1", "status": "In Review", "updated": "2026-03-01T10:00:00Z"}
    )
    assert observation.case_ref == "CPQ-1"
    assert observation.activity_key == "act.legal_review"
    assert observation.raw_activity == "In Review"
    assert observation.occurred_at == datetime(2026, 3, 1, 10, 0, tzinfo=UTC)


def test_normalize_keeps_raw_activity_when_the_map_has_no_entry() -> None:
    """An unmapped status is the `missing_in_graph` signal (docs/08 section
    2.1), not a record to drop.
    """
    observation = normalize(
        _spec(), {"key": "CPQ-2", "status": "Escalated", "updated": "2026-03-01T10:00:00Z"}
    )
    assert observation.activity_key is None
    assert observation.raw_activity == "Escalated"


def test_normalize_leaves_actor_columns_null_without_an_actor_field() -> None:
    observation = normalize(
        _spec(), {"key": "CPQ-3", "status": "In Review", "updated": "2026-03-01T10:00:00Z"}
    )
    assert observation.actor_hash is None
    assert observation.actor_role_key is None


def test_normalize_leaves_actor_role_key_null_for_an_unmapped_actor() -> None:
    spec = _spec(actor_field="assignee", actor_role_map={"known": "role.deal_desk"})
    observation = normalize(
        spec,
        {
            "key": "CPQ-4",
            "status": "In Review",
            "updated": "2026-03-01T10:00:00Z",
            "assignee": "somebody-else",
        },
        actor_hasher=lambda raw: f"hashed:{raw}",
    )
    assert observation.actor_role_key is None, (
        "an unmapped actor is invisible to actor drift by design -- silence, "
        "not a wrong-role false positive"
    )
    assert observation.actor_hash == "hashed:somebody-else"


def test_normalize_requires_a_hasher_when_the_mapping_declares_an_actor_field() -> None:
    spec = _spec(actor_field="assignee")
    with pytest.raises(RecordError, match="hasher"):
        normalize(
            spec,
            {
                "key": "CPQ-5",
                "status": "In Review",
                "updated": "2026-03-01T10:00:00Z",
                "assignee": "a",
            },
        )


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("2026-03-01T10:00:00Z", datetime(2026, 3, 1, 10, 0, tzinfo=UTC)),
        ("2026-03-01T10:00:00+00:00", datetime(2026, 3, 1, 10, 0, tzinfo=UTC)),
        ("2026-03-01T12:00:00+02:00", datetime(2026, 3, 1, 10, 0, tzinfo=UTC)),
        ("2026-03-01T10:00:00", datetime(2026, 3, 1, 10, 0, tzinfo=UTC)),  # naive reads as UTC
        (1772359200, datetime(2026, 3, 1, 10, 0, tzinfo=UTC)),  # epoch seconds
        (1772359200000, datetime(2026, 3, 1, 10, 0, tzinfo=UTC)),  # epoch millis
    ],
)
def test_normalize_parses_every_documented_timestamp_shape(raw: Any, expected: datetime) -> None:
    observation = normalize(_spec(), {"key": "CPQ-6", "status": "In Review", "updated": raw})
    assert observation.occurred_at == expected


@pytest.mark.parametrize("raw", ["not a date", None, True])
def test_normalize_raises_record_error_on_an_unparseable_timestamp(raw: Any) -> None:
    with pytest.raises(RecordError, match="updated"):
        normalize(_spec(), {"key": "CPQ-7", "status": "In Review", "updated": raw})


@pytest.mark.parametrize("missing_field", ["key", "status"])
def test_normalize_raises_record_error_on_a_missing_required_field(missing_field: str) -> None:
    payload = {"key": "CPQ-8", "status": "In Review", "updated": "2026-03-01T10:00:00Z"}
    del payload[missing_field]
    with pytest.raises(RecordError, match=missing_field):
        normalize(_spec(), payload)


def test_normalize_converts_millisecond_durations_to_seconds() -> None:
    """docs/08 section 2.4's generic event-stream example carries `duration_ms`."""
    spec = _spec(duration_field="duration_ms", duration_unit="ms")
    observation = normalize(
        spec,
        {
            "key": "CPQ-9",
            "status": "In Review",
            "updated": "2026-03-01T10:00:00Z",
            "duration_ms": 90_500,
        },
    )
    assert observation.duration_seconds == 90


def test_normalize_copies_attributes_fields() -> None:
    spec = _spec(attributes_fields=["priority", "fields.team"])
    observation = normalize(
        spec,
        {
            "key": "CPQ-10",
            "status": "In Review",
            "updated": "2026-03-01T10:00:00Z",
            "priority": "High",
            "fields": {"team": "emea"},
        },
    )
    assert observation.attributes == {"priority": "High", "fields.team": "emea"}


# ===========================================================================
# compute_dedup_key
# ===========================================================================
_BASE = {
    "case_ref": "CPQ-1",
    "raw_activity": "In Review",
    "occurred_at": datetime(2026, 3, 1, 10, 0, tzinfo=UTC),
    "actor_hash": "abc123",
}


def test_dedup_key_is_deterministic() -> None:
    assert compute_dedup_key(**_BASE) == compute_dedup_key(**_BASE)


@pytest.mark.parametrize(
    ("field_name", "different"),
    [
        ("case_ref", "CPQ-2"),
        ("raw_activity", "Closed"),
        ("occurred_at", datetime(2026, 3, 1, 10, 0, 1, tzinfo=UTC)),
        ("actor_hash", "def456"),
    ],
)
def test_dedup_key_changes_with_every_component(field_name: str, different: Any) -> None:
    assert compute_dedup_key(**{**_BASE, field_name: different}) != compute_dedup_key(**_BASE)


def test_dedup_key_is_stable_across_equivalent_timezone_spellings() -> None:
    """The same instant written `+00:00` and `+02:00` must hash identically --
    otherwise the dedup index lets the duplicate straight through, which is
    the whole failure db/014 exists to prevent.
    """
    same_instant = datetime(2026, 3, 1, 12, 0, tzinfo=timezone(timedelta(hours=2)))
    assert compute_dedup_key(**{**_BASE, "occurred_at": same_instant}) == compute_dedup_key(**_BASE)


def test_dedup_key_distinguishes_present_from_absent_actor() -> None:
    assert compute_dedup_key(**{**_BASE, "actor_hash": None}) != compute_dedup_key(**_BASE)
