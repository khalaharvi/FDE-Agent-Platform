"""mapping.py -- the `sor.adapter.mapping` jsonb schema, and normalisation.

`mapping` "is the entire adapter-specific contract" (docs/08 §2), and it is
authored by a human at onboarding, never inferred by an agent (docs/08 §2.5 --
an inferred mapping is derived from the same graph the detectors compare
against, so drift detection against it cannot detect the thing it exists to
detect). This module is the parser for that contract and the pure function
that turns one raw SoR record into one `sor.observation` row.

Everything here is synchronous and side-effect free. That is what lets the
whole mapping surface -- every worked example in docs/08 -- be tested without a
database, a network, or AWS.

The schema
-----------
Shared keys, interpreted identically by every adapter kind:

| key | required | meaning |
|---|---|---|
| `case_id_field`      | yes | path to the business case id (ticket key, opportunity id) -> `case_ref` |
| `activity_field`     | yes | path to the raw activity/status value -> `raw_activity` |
| `activity_map`       | yes | `{raw value: activity_key}`; an unmapped value yields `activity_key = NULL` and keeps `raw_activity` (this is the `missing_in_graph` signal, not a dropped record) |
| `timestamp_field`    | yes | path to the event time -> `occurred_at` |
| `actor_field`        | no  | path to an actor identifier. Its presence is what makes a salt mandatory (see `hashing`) |
| `actor_role_map`     | no  | `{raw actor id: role node_key}` -> `actor_role_key`; unmapped means NULL, i.e. invisible to actor drift by design (docs/08 §2.1) |
| `duration_field`     | no  | path to a duration -> `duration_seconds` |
| `duration_unit`      | no  | `"s"` (default) or `"ms"` -- docs/08 §2.4's example field is `duration_ms` |
| `system_object_field`| no  | path to the SoR object the activity touched -> `system_object_key` |
| `attributes_fields`  | no  | list of paths copied verbatim into `attributes` |

Kind-specific invocation config lives under namespaced sub-objects
(`request`, `stream`, `cdc`) so that adding a kind never widens the shared
vocabulary. Credentials are NEVER in `mapping` -- they are in the Secrets
Manager secret named by `sor.adapter.secret_arn`.

Field paths are dotted (`assignee.accountId`); an integer segment indexes a
list (`changelog.0.field`). That is a ~15-line stdlib resolver rather than a
JSONPath dependency because no example in docs/08 needs more than dotted
access, and this repo is deliberately dependency-averse.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable

__all__ = [
    "MappingError",
    "MappingSpec",
    "NormalizedObservation",
    "RecordError",
    "compute_dedup_key",
    "normalize",
    "resolve_path",
]

DURATION_UNITS = ("s", "ms")

# Anything numeric larger than this is read as epoch milliseconds rather than
# epoch seconds. 10^11 seconds is the year 5138; 10^11 milliseconds is 1973.
# Any real event timestamp is unambiguous under that split.
_EPOCH_MILLIS_THRESHOLD = 1e11


class MappingError(ValueError):
    """The `sor.adapter.mapping` jsonb is not a usable contract.

    Raised by `MappingSpec.parse` before any record is fetched, naming every
    problem at once rather than one per round trip.
    """


class RecordError(ValueError):
    """One raw record could not be normalised.

    Collected into `IngestStats.record_errors` and skipped; one malformed
    record never aborts a batch. Messages name FIELDS, never VALUES -- an
    actor identifier must not reach a log line or an exception string (see
    `hashing`).
    """


def resolve_path(payload: Any, path: str) -> Any:
    """Resolve a dotted path against nested dicts and lists.

    Returns `None` for any missing key, out-of-range index, or attempt to
    index into a scalar. Absence is a normal outcome here -- an optional
    mapped field that a particular record does not carry is a NULL column,
    not an error -- so this never raises.
    """
    current = payload
    for segment in path.split("."):
        if isinstance(current, dict):
            current = current.get(segment)
        elif isinstance(current, list):
            try:
                current = current[int(segment)]
            except (ValueError, IndexError):
                return None
        else:
            return None
        if current is None:
            return None
    return current


def _parse_timestamp(value: Any, field_name: str) -> datetime:
    """ISO-8601 (Z-tolerant) or a numeric epoch, always returned as UTC.

    Mirrors `fde_mcp.tools._base.parse_timestamp` for the string case and adds
    the numeric case, because SoR event streams commonly carry epoch millis.
    A naive timestamp is read as UTC rather than as local time: the column is
    `timestamptz`, the platform's whole time axis is UTC, and guessing the
    poller's local zone would silently shift every observation by the
    container's offset.
    """
    if isinstance(value, bool):  # bool is an int subclass; never a timestamp
        msg = f"field {field_name!r} is a boolean, not a timestamp"
        raise RecordError(msg)
    if isinstance(value, int | float):
        seconds = float(value)
        if abs(seconds) >= _EPOCH_MILLIS_THRESHOLD:
            seconds /= 1000.0
        return datetime.fromtimestamp(seconds, tz=UTC)
    if isinstance(value, str):
        text = value.strip()
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError as exc:
            msg = f"field {field_name!r} is not an ISO-8601 timestamp or epoch number"
            raise RecordError(msg) from exc
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return parsed.astimezone(UTC)
    msg = f"field {field_name!r} is missing or not a timestamp"
    raise RecordError(msg)


def _parse_duration(value: Any, unit: str, field_name: str) -> int | None:
    if value is None:
        return None
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        msg = f"field {field_name!r} is not numeric, cannot be a duration"
        raise RecordError(msg) from None
    if unit == "ms":
        numeric /= 1000.0
    return round(numeric)


@dataclass(frozen=True, slots=True)
class MappingSpec:
    """A parsed, validated `sor.adapter.mapping`."""

    case_id_field: str
    activity_field: str
    activity_map: dict[str, str]
    timestamp_field: str
    actor_field: str | None = None
    actor_role_map: dict[str, str] = field(default_factory=dict)
    duration_field: str | None = None
    duration_unit: str = "s"
    system_object_field: str | None = None
    attributes_fields: tuple[str, ...] = ()
    # Namespaced per-kind invocation config. Never credentials.
    request: dict[str, Any] = field(default_factory=dict)
    stream: dict[str, Any] = field(default_factory=dict)
    cdc: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def parse(cls, mapping: dict[str, Any]) -> MappingSpec:
        """Validate `mapping`, reporting EVERY problem in one message.

        One error at a time would make fixing a hand-authored mapping a
        round-trip per typo, and this is a file a human edits at onboarding.
        """
        problems: list[str] = []

        def _required_str(key: str) -> str:
            value = mapping.get(key)
            if not isinstance(value, str) or not value:
                problems.append(f"{key!r} is required and must be a non-empty string")
                return ""
            return value

        def _optional_str(key: str) -> str | None:
            value = mapping.get(key)
            if value is None:
                return None
            if not isinstance(value, str) or not value:
                problems.append(f"{key!r} must be a non-empty string when present")
                return None
            return value

        def _str_map(key: str, *, required: bool) -> dict[str, str]:
            value = mapping.get(key)
            if value is None:
                if required:
                    problems.append(f"{key!r} is required and must be an object of raw -> key")
                return {}
            if not isinstance(value, dict) or not all(
                isinstance(k, str) and isinstance(v, str) for k, v in value.items()
            ):
                problems.append(f"{key!r} must be an object mapping strings to strings")
                return {}
            return dict(value)

        def _sub_object(key: str) -> dict[str, Any]:
            value = mapping.get(key)
            if value is None:
                return {}
            if not isinstance(value, dict):
                problems.append(f"{key!r} must be an object when present")
                return {}
            return dict(value)

        case_id_field = _required_str("case_id_field")
        activity_field = _required_str("activity_field")
        timestamp_field = _required_str("timestamp_field")
        activity_map = _str_map("activity_map", required=True)
        actor_role_map = _str_map("actor_role_map", required=False)

        duration_unit = mapping.get("duration_unit", "s")
        if duration_unit not in DURATION_UNITS:
            problems.append(
                f"'duration_unit' must be one of {DURATION_UNITS!r}, got {duration_unit!r}"
            )
            duration_unit = "s"

        raw_attribute_fields = mapping.get("attributes_fields", [])
        attributes_fields: tuple[str, ...] = ()
        if raw_attribute_fields:
            if not isinstance(raw_attribute_fields, list) or not all(
                isinstance(p, str) for p in raw_attribute_fields
            ):
                problems.append("'attributes_fields' must be a list of dotted path strings")
            else:
                attributes_fields = tuple(raw_attribute_fields)

        actor_field = _optional_str("actor_field")
        if actor_role_map and not actor_field:
            problems.append(
                "'actor_role_map' is set but 'actor_field' is not, so no actor "
                "identifier is ever read and every actor_role_key would be NULL"
            )

        if problems:
            msg = "invalid sor.adapter.mapping: " + "; ".join(problems)
            raise MappingError(msg)

        return cls(
            case_id_field=case_id_field,
            activity_field=activity_field,
            activity_map=activity_map,
            timestamp_field=timestamp_field,
            actor_field=actor_field,
            actor_role_map=actor_role_map,
            duration_field=_optional_str("duration_field"),
            duration_unit=duration_unit,
            system_object_field=_optional_str("system_object_field"),
            attributes_fields=attributes_fields,
            request=_sub_object("request"),
            stream=_sub_object("stream"),
            cdc=_sub_object("cdc"),
        )


@dataclass(frozen=True, slots=True)
class NormalizedObservation:
    """One `sor.observation` row, ready to insert.

    Note what is NOT here: the raw actor identifier. It exists only as a local
    inside `normalize`, is immediately replaced by its HMAC, and is never
    stored on this object, so it cannot reach a log line, a repr, a trace, or
    a debugger frame further down the pipeline. That is enforced by a test
    (`test_hashing.py`), not by convention.
    """

    case_ref: str
    activity_key: str | None
    raw_activity: str
    actor_hash: str | None
    actor_role_key: str | None
    system_object_key: str | None
    occurred_at: datetime
    duration_seconds: int | None
    attributes: dict[str, Any]
    dedup_key: str


def compute_dedup_key(
    *, case_ref: str, raw_activity: str, occurred_at: datetime, actor_hash: str | None
) -> str:
    """`sha256(case_ref|raw_activity|occurred_at|actor_hash)`, per db/014.

    `adapter_id` is deliberately NOT in the hash -- it lives in the unique
    index instead -- so the same record arriving through two different
    adapters stays two observations, because that is two independent
    measurements of the same event.

    `occurred_at` is normalised to UTC with microsecond precision before
    hashing. Without that, the same instant expressed as `+00:00` and as
    `+02:00`, or with and without trailing zeros, would hash differently and
    the dedup index would let the duplicate through.
    """
    stamp = occurred_at.astimezone(UTC).isoformat(timespec="microseconds")
    material = f"{case_ref}|{raw_activity}|{stamp}|{actor_hash or ''}"
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def normalize(
    spec: MappingSpec,
    payload: dict[str, Any],
    *,
    actor_hasher: Callable[[str], str] | None = None,
) -> NormalizedObservation:
    """Map one raw SoR record onto one `sor.observation` row.

    Raises `RecordError` (never anything else) when the record lacks the
    fields the table requires -- a case ref, an activity value, a timestamp.
    The caller collects those and moves on; a single malformed record must not
    cost a whole batch.

    `actor_hasher` is required when `spec.actor_field` is set and must be
    absent otherwise. It is passed in rather than resolved here so that salt
    resolution -- the one step that can fail closed, and the one that touches
    the network -- happens once per run, before any record is fetched.
    """
    case_value = resolve_path(payload, spec.case_id_field)
    if case_value is None or (isinstance(case_value, str) and not case_value.strip()):
        msg = f"field {spec.case_id_field!r} (case_id_field) is missing or empty"
        raise RecordError(msg)
    case_ref = str(case_value)

    activity_value = resolve_path(payload, spec.activity_field)
    if activity_value is None or (isinstance(activity_value, str) and not activity_value.strip()):
        msg = f"field {spec.activity_field!r} (activity_field) is missing or empty"
        raise RecordError(msg)
    raw_activity = str(activity_value)
    # An unmapped raw value is NOT an error: NULL activity_key with
    # raw_activity kept is precisely what feeds the missing_in_graph detector
    # (docs/08 §2.1), and dropping it would hide the drift.
    activity_key = spec.activity_map.get(raw_activity)

    occurred_at = _parse_timestamp(
        resolve_path(payload, spec.timestamp_field), spec.timestamp_field
    )

    actor_hash: str | None = None
    actor_role_key: str | None = None
    if spec.actor_field is not None:
        if actor_hasher is None:
            msg = (
                f"mapping declares actor_field {spec.actor_field!r} but no actor "
                "hasher was supplied; refusing to store an unhashed identifier"
            )
            raise RecordError(msg)
        raw_actor = resolve_path(payload, spec.actor_field)
        if raw_actor is not None:
            # `raw_actor` dies here. Everything downstream sees only the HMAC.
            actor_identifier = str(raw_actor)
            actor_hash = actor_hasher(actor_identifier)
            actor_role_key = spec.actor_role_map.get(actor_identifier)
            del actor_identifier, raw_actor

    duration_seconds = None
    if spec.duration_field is not None:
        duration_seconds = _parse_duration(
            resolve_path(payload, spec.duration_field), spec.duration_unit, spec.duration_field
        )

    system_object_key = None
    if spec.system_object_field is not None:
        raw_object = resolve_path(payload, spec.system_object_field)
        system_object_key = None if raw_object is None else str(raw_object)

    attributes: dict[str, Any] = {}
    for path in spec.attributes_fields:
        value = resolve_path(payload, path)
        if value is not None:
            attributes[path] = value

    return NormalizedObservation(
        case_ref=case_ref,
        activity_key=activity_key,
        raw_activity=raw_activity,
        actor_hash=actor_hash,
        actor_role_key=actor_role_key,
        system_object_key=system_object_key,
        occurred_at=occurred_at,
        duration_seconds=duration_seconds,
        attributes=attributes,
        dedup_key=compute_dedup_key(
            case_ref=case_ref,
            raw_activity=raw_activity,
            occurred_at=occurred_at,
            actor_hash=actor_hash,
        ),
    )
