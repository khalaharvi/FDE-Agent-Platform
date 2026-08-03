"""forms.py -- a declared shape, rendered as fields a person can fill in.

The console had two `<textarea>`s asking an operator to type a JSON object:
the run-start `input` and the human-step `response`. Both of them stood in
front of a shape the database already knows -- `wf.step.human_schema` is
described in db/006 as "the accepted answers" -- so the textarea was never
carrying information the server lacked. It was carrying the cost of not
looking.

This module is the lookup. `fields_from_schema` turns a `human_schema` into
`Field`s, a template renders them, and `values_from_form` reads them back as
the same JSON object the textarea used to produce. The launcher
(`service/agents.py`) builds `Field`s directly rather than from a schema, so
both halves of the console describe a form the same way and one Jinja macro
renders both.

Two dialects, because two dialects exist in this repository
------------------------------------------------------------
`human_schema` is a bare `jsonb` column with no CHECK constraint, and the
rows written so far use two different conventions:

    {"approved": "boolean"}                          -- db/tests/smoke_test.sql:765
    {"type": "object", "properties": {...}}          -- JSON Schema

Both are read. Guessing between them is safe because they cannot collide: a
JSON Schema object says so in `type`/`properties`, and a flat map's values
are type NAMES or small specs. What is not safe is guessing badly and
silently dropping a field, so anything this module does not recognise makes
it return NOTHING -- and the caller falls back to the JSON textarea, clearly
labelled as the fallback. A half-generated form that quietly discards the
field an operator did not see is worse than the textarea it replaced.

`assignee` is not a field
--------------------------
`runner._assignee` reads `human_schema.assignee` to decide who a step parks
on. It is metadata about the step, not a question for the person answering
it, and rendering it as an input would invite an operator to overwrite the
routing with their own answer.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from http import HTTPStatus
from typing import Any

from fde_gate.http import GateError

__all__ = [
    "FIELD_PREFIX",
    "Field",
    "fields_from_schema",
    "values_from_form",
]

#: Prefixed onto every generated control's `name`. See `Field.input_name`.
FIELD_PREFIX = "f_"

# Keys of a `human_schema` that describe the STEP rather than the answer.
# See the module docstring.
_RESERVED_KEYS = frozenset({"assignee"})

# Every spelling of false a `<select>` or a checkbox can submit, matched
# case-insensitively. The list is wider than any one widget needs because
# the reader is the last line: a control whose option values stop agreeing
# with it must still not turn "false" into true.
_FALSY_SUBMISSIONS = frozenset({"", "0", "false", "no", "off"})

# JSON Schema keywords at the top level of an object schema. Present so the
# flat-map reader can tell "this is a JSON Schema" from "this is a map of
# field names to types" without either one having to be declared.
_SCHEMA_KEYWORDS = frozenset({"type", "properties", "required", "title", "description"})

# The JSON types this module knows how to ask for. Anything else -- a nested
# object, an array of them -- means no form; see the module docstring.
_SUPPORTED_TYPES = frozenset({"string", "boolean", "integer", "number", "array"})

# What an array's entries may be. A list typed one-per-line into a box is a
# list of scalars; an array of objects is a table, and a text box that
# claimed to collect one would be collecting prose.
_SUPPORTED_ITEM_TYPES = frozenset({"string", "integer", "number"})


@dataclass(frozen=True, slots=True)
class Field:
    """One control on a console form, and how to read its value back.

    Attributes:
        name: The form field name, and the key it becomes in the resulting
            JSON object.
        label: What the operator reads next to the control.
        value_type: One of `_SUPPORTED_TYPES`. Decides both the widget and
            the coercion `values_from_form` applies, so the two cannot
            disagree about what a control meant.
        item_type: What each entry of an `array` becomes. Only "string",
            "integer" and "number" -- a list an operator types one per line
            cannot carry objects, which is why an array of them produces no
            form at all rather than a box that would lose their structure.
        hint: One line under the control. Empty renders nothing.
        required: Renders the `required` attribute AND is re-checked on the
            server -- a browser that skips validation is a browser, not an
            authorisation boundary.
        options: `(submitted value, label)` pairs. Non-empty renders a
            `<select>`, whatever `value_type` says. The VALUE half is
            whatever `values_from_form`'s reader for `value_type` parses
            back to the schema's member -- not the member's `str()`. They
            differ for booleans, and that difference was a bug: `str(False)`
            is "False", which a reader testing against "false" read as true.
        placeholder: Shown in an empty text control.
        rows: Non-zero renders a `<textarea>` that many rows tall instead of
            an `<input>`. Only meaningful for `value_type == "string"`.
        value: Pre-filled contents, used to echo a submission back when a
            launch is refused -- the material in a paste box is the one thing
            on these forms that is expensive to lose.
    """

    name: str
    label: str
    value_type: str = "string"
    item_type: str = "string"
    hint: str = ""
    required: bool = False
    options: tuple[tuple[str, str], ...] = ()
    placeholder: str = ""
    rows: int = 0
    value: str = ""

    @property
    def input_name(self) -> str:
        """The `name` this field's control carries in the HTML form.

        Prefixed, because a generated name comes from `wf.step.human_schema`
        -- authored by an agent through `wf_draft` -- and is posted into a
        form that already carries control fields of its own: `workflow_id`
        on the run-start form, `run_id` and `action` on the answer form.
        Form parsing keeps the LAST value for a repeated key
        (`http.py:parse_apigw_event`), and the generated control renders
        after the hidden one, so an unprefixed `workflow_id` in a schema
        would silently start a DIFFERENT workflow than the operator picked.

        Separating the namespaces is the fix rather than reserving names,
        because the reserved list would have to be right forever and this
        only has to be right once. `values_from_form` strips it back off.
        """
        return f"{FIELD_PREFIX}{self.name}"

    def with_value(self, value: str) -> Field:
        """A copy carrying what the operator typed, for a re-render."""
        return replace(self, value=value)


@dataclass(frozen=True, slots=True)
class _Property:
    """One property of a schema, before it becomes a `Field`."""

    name: str
    value_type: str
    item_type: str = "string"
    title: str = ""
    description: str = ""
    #: The schema's members, as authored -- Python `True`, not "True". They
    #: are turned into option values by `_option` once the type is known.
    enum: tuple[Any, ...] = ()
    required: bool = False


def fields_from_schema(schema: Any) -> list[Field]:
    """Fields for `schema`, or an empty list when it cannot drive a form.

    An empty list is the caller's signal to render the labelled JSON
    fallback. It is returned for a schema that is absent, is not an object,
    declares no properties, or declares one this module cannot ask for --
    see the module docstring on why "partly" is not an option.
    """
    properties = _properties(schema)
    if properties is None:
        return []
    return [_to_field(prop) for prop in properties]


def _properties(schema: Any) -> list[_Property] | None:
    """Parse either dialect into properties, or None if neither applies."""
    if not isinstance(schema, dict) or not schema:
        return None
    if "properties" in schema or schema.get("type") == "object":
        return _json_schema_properties(schema)
    return _flat_map_properties(schema)


def _json_schema_properties(schema: dict[str, Any]) -> list[_Property] | None:
    raw = schema.get("properties")
    if not isinstance(raw, dict) or not raw:
        return None
    required = schema.get("required")
    required_names = set(required) if isinstance(required, list) else set()

    out: list[_Property] = []
    for name, spec in raw.items():
        if name in _RESERVED_KEYS:
            continue
        prop = _property_from_spec(str(name), spec, required=str(name) in required_names)
        if prop is None:
            return None
        out.append(prop)
    return out or None


def _flat_map_properties(schema: dict[str, Any]) -> list[_Property] | None:
    """`{"approved": "boolean"}` and its slightly-richer cousins.

    A key whose value is a bare string is that key's TYPE -- the smoke test's
    convention. A key whose value is an object is read as a property spec, so
    `{"approved": {"type": "boolean", "title": "Approve?"}}` works without a
    `properties` wrapper. Everything in this dialect is required: a shape
    with no `required` list and no way to express optionality would otherwise
    make every field optional, and a workflow author who wrote down a field
    meant it.
    """
    if _SCHEMA_KEYWORDS.intersection(schema):
        # A JSON Schema that reached here declares keywords but no usable
        # `properties` -- `{"type": "object"}` on its own, say. Reading its
        # keywords as field names would ask an operator to fill in "type".
        return None

    out: list[_Property] = []
    for name, spec in schema.items():
        if name in _RESERVED_KEYS:
            continue
        prop = _property_from_spec(str(name), spec, required=True)
        if prop is None:
            return None
        out.append(prop)
    return out or None


def _property_from_spec(name: str, spec: Any, *, required: bool) -> _Property | None:
    if isinstance(spec, str):
        value_type = spec.strip().lower()
        return (
            _Property(name=name, value_type=value_type, required=required)
            if value_type in _SUPPORTED_TYPES
            else None
        )
    if not isinstance(spec, dict):
        return None

    declared = spec.get("type")
    # A nullable field arrives as ["string", "null"]; the null carries no
    # question for the operator, so it is dropped and what remains has to be
    # exactly one type this module can ask for.
    if isinstance(declared, list):
        candidates = [str(t) for t in declared if t != "null"]
        declared = candidates[0] if len(candidates) == 1 else None
    value_type = str(declared).strip().lower() if isinstance(declared, str) else ""

    enum = spec.get("enum")
    if isinstance(enum, list) and enum:
        return _enum_property(
            name, spec, declared=declared, value_type=value_type, enum=enum, required=required
        )
    if value_type not in _SUPPORTED_TYPES:
        return None
    item_type = _item_type(spec) if value_type == "array" else "string"
    if item_type is None:
        return None
    return _Property(
        name=name,
        value_type=value_type,
        item_type=item_type,
        title=str(spec.get("title") or ""),
        description=str(spec.get("description") or ""),
        required=required,
    )


def _enum_property(
    name: str,
    spec: dict[str, Any],
    *,
    declared: Any,
    value_type: str,
    enum: list[Any],
    required: bool,
) -> _Property | None:
    """A property whose answers are a fixed list, or None if it cannot be one.

    An enum renders as a `<select>` whose options are exactly the accepted
    answers -- but it does NOT excuse the type from the gate every other
    property passes. It used to: a declared type this module cannot read was
    quietly downgraded to "string", which made an enum a hole in the
    whole-or-nothing rule. An enum with NO declared type is still strings,
    which is what its members are, and nothing has been bypassed.
    """
    if isinstance(declared, str) and value_type not in _SUPPORTED_TYPES:
        return None
    if value_type == "array":
        # A choice between lists. A <select> whose value is one line of text
        # cannot express it, and splitting that line would invent a list the
        # enum never offered.
        return None
    return _Property(
        name=name,
        value_type=value_type if value_type in _SUPPORTED_TYPES else "string",
        title=str(spec.get("title") or ""),
        description=str(spec.get("description") or ""),
        enum=tuple(enum),
        required=required,
    )


def _item_type(spec: dict[str, Any]) -> str | None:
    """What an array's entries are, or None when they cannot be typed in.

    An `items` that is absent means "unconstrained", which this reads as a
    list of strings -- the only thing a one-per-line box can produce without
    inventing structure. An `items` naming objects, or naming a tuple of
    per-position schemas, is a table and gets no form at all.
    """
    items = spec.get("items")
    if items is None:
        return "string"
    if not isinstance(items, dict):
        return None
    declared = items.get("type")
    if not isinstance(declared, str):
        return None
    item_type = declared.strip().lower()
    return item_type if item_type in _SUPPORTED_ITEM_TYPES else None


def _to_field(prop: _Property) -> Field:
    return Field(
        name=prop.name,
        label=prop.title or _humanise(prop.name),
        value_type=prop.value_type,
        item_type=prop.item_type,
        hint=prop.description or _type_hint(prop),
        required=prop.required and prop.value_type != "boolean",
        options=tuple(_option(prop.value_type, item) for item in prop.enum),
        rows=0,
    )


def _option(value_type: str, item: Any) -> tuple[str, str]:
    """One `<select>` option: what it submits, and what it reads as.

    The two halves are not the same string. The value has to survive
    `values_from_form`'s reader for `value_type` and come back as `item`;
    the label is what the operator sees. For every type but boolean those
    coincide. For boolean they cannot: `str(False)` is "False", which is
    not any spelling of false a checkbox submits, so the reader saw a
    non-empty string and returned true. The label is JSON's spelling
    because JSON is what the author wrote the schema in.
    """
    if value_type == "boolean":
        truthy = item is True or (isinstance(item, str) and item.strip().lower() == "true")
        return ("1" if truthy else "", "true" if truthy else "false")
    return (str(item), str(item))


def _humanise(name: str) -> str:
    """ "root_process_key" -> "Root process key". The field name is the only
    label a bare `{"approved": "boolean"}` offers, and showing it verbatim
    puts a snake_case identifier in front of someone who did not write it.
    """
    words = name.replace("_", " ").replace("-", " ").strip()
    return words[:1].upper() + words[1:] if words else name


def _type_hint(prop: _Property) -> str:
    if prop.value_type == "array":
        return "One per line, or separated by commas."
    if prop.value_type == "integer":
        return "A whole number."
    if prop.value_type == "number":
        return "A number."
    return ""


def values_from_form(fields: list[Field], form: dict[str, str]) -> dict[str, Any]:
    """Read `fields` back out of a submitted form as a JSON object.

    Read under `Field.input_name` and returned under `Field.name`: the
    prefix exists only between the macro and here, so the object this
    produces is keyed by what the schema declared.

    Required fields are re-checked here rather than left to the browser, and
    a refusal names the field's LABEL -- the operator never saw the key.
    An optional field left blank is omitted entirely rather than sent as an
    empty string: `$.input.note` being absent and being "" are different
    things to a jsonpath, and only one of them means "not answered".
    """
    values: dict[str, Any] = {}
    for spec in fields:
        raw = form.get(spec.input_name, "")
        if spec.value_type == "boolean":
            # An unchecked checkbox is not submitted at all, which is exactly
            # how HTML says false. Booleans are therefore always present.
            values[spec.name] = raw.strip().lower() not in _FALSY_SUBMISSIONS
            continue
        raw = raw.strip()
        if not raw:
            if spec.required:
                raise GateError(HTTPStatus.BAD_REQUEST, f"{spec.label} is required.")
            continue
        values[spec.name] = _coerce(spec, raw)
    return values


def _coerce(spec: Field, raw: str) -> Any:
    if spec.value_type == "array":
        parts = [part.strip() for chunk in raw.splitlines() for part in chunk.split(",")]
        return [_scalar(spec, part, spec.item_type) for part in parts if part]
    return _scalar(spec, raw, spec.value_type)


def _scalar(spec: Field, raw: str, value_type: str) -> Any:
    if value_type not in ("integer", "number"):
        return raw
    try:
        return int(raw) if value_type == "integer" else float(raw)
    except ValueError:
        noun = "whole number" if value_type == "integer" else "number"
        # Names the entry rather than the whole box, because "Signal numbers
        # must be a whole number" reads as though the list itself were wrong
        # when one line of six is the thing to fix.
        subject = f"every entry of {spec.label}" if spec.value_type == "array" else spec.label
        raise GateError(
            HTTPStatus.BAD_REQUEST, f"{subject} must be a {noun}, not {raw!r}."
        ) from None
