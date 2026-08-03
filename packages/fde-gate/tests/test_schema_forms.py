"""Generated forms: what a declared shape turns into, and when it does not.

No database. `human_schema` is a bare `jsonb` column with no CHECK
constraint, so what actually matters here is behaviour on the shapes that
already exist in this repository and on the shapes nobody has written yet --
both of which are literals.

The claim these tests defend is narrow and load-bearing: a schema either
produces a COMPLETE form or produces nothing at all. A schema that generated
fields for three of its four properties would silently drop the fourth from
every answer, and the operator would never see the field that went missing.
"""

from __future__ import annotations

from typing import Any

import pytest

from fde_gate import ui
from fde_gate.forms import FIELD_PREFIX, Field, fields_from_schema, values_from_form
from fde_gate.http import GateError

# The shape db/tests/smoke_test.sql:765 actually writes.
SMOKE_SCHEMA: dict[str, Any] = {"approved": "boolean"}

# The shape packages/fde-mcp/tests/test_playbook_render.py writes.
JSON_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"approved": {"type": "boolean"}},
}


def _names(fields: list[Field]) -> list[str]:
    return [f.name for f in fields]


def test_the_flat_map_dialect_from_the_smoke_test_becomes_one_field() -> None:
    (field,) = fields_from_schema(SMOKE_SCHEMA)
    assert field.name == "approved"
    assert field.value_type == "boolean"
    # The key is the only label this dialect offers, so it is made readable
    # rather than shown as the identifier a workflow author typed.
    assert field.label == "Approved"


def test_the_json_schema_dialect_becomes_the_same_field() -> None:
    """Two conventions, one form. Which one a workflow author used is not a
    thing the operator answering the step should be able to tell.
    """
    assert fields_from_schema(JSON_SCHEMA) == fields_from_schema(SMOKE_SCHEMA)


def test_titles_descriptions_and_enums_are_carried_through() -> None:
    (field,) = fields_from_schema(
        {
            "type": "object",
            "required": ["disposition"],
            "properties": {
                "disposition": {
                    "type": "string",
                    "title": "How did it go?",
                    "description": "Pick the closest match.",
                    "enum": ["clean", "reworked", "abandoned"],
                }
            },
        }
    )
    assert field.label == "How did it go?"
    assert field.hint == "Pick the closest match."
    assert field.required
    assert field.options == (
        ("clean", "clean"),
        ("reworked", "reworked"),
        ("abandoned", "abandoned"),
    )


BOOLEAN_ENUM: dict[str, Any] = {
    "type": "object",
    "properties": {"approved": {"type": "boolean", "enum": [True, False]}},
}


def test_a_boolean_enum_that_says_false_means_false() -> None:
    """The demonstrated bug. A boolean with an `enum` skipped the type gate:
    its options were `str()`-ed to "True"/"False" while `value_type` stayed
    boolean, and the reader's falsy set held the lower-case "false" -- so
    picking False submitted "False", matched nothing, and recorded True. A
    reviewer approving nothing was recorded as approving.
    """
    (field,) = fields_from_schema(BOOLEAN_ENUM)
    assert field.value_type == "boolean"

    (false_value,) = [value for value, label in field.options if label == "false"]
    assert values_from_form([field], {field.input_name: false_value}) == {"approved": False}

    (true_value,) = [value for value, label in field.options if label == "true"]
    assert values_from_form([field], {field.input_name: true_value}) == {"approved": True}


def test_an_enum_option_value_is_what_its_own_reader_parses_back() -> None:
    """The general rule the case above is one instance of: an option's VALUE
    has to survive `values_from_form`, whatever the declared type. Labels are
    for the operator; values are for the reader.
    """
    for schema, expected in (
        ({"n": {"type": "integer", "enum": [1, 2]}}, [1, 2]),
        ({"r": {"type": "number", "enum": [0.5, 1.5]}}, [0.5, 1.5]),
        ({"s": {"type": "string", "enum": ["a", "b"]}}, ["a", "b"]),
        ({"b": {"type": "boolean", "enum": [True, False]}}, [True, False]),
    ):
        (field,) = fields_from_schema(schema)
        read_back = [
            values_from_form([field], {field.input_name: value})[field.name]
            for value, _ in field.options
        ]
        assert read_back == expected, f"{schema} round-tripped to {read_back}"


@pytest.mark.parametrize("submitted", ["", "0", "false", "False", "FALSE", "no", "off", " False "])
def test_every_spelling_of_false_reads_as_false(submitted: str) -> None:
    """Belt and braces behind the option-value fix. The reader is the last
    line: a control whose values stop agreeing with it must still not turn a
    plain "False" into true.
    """
    (field,) = fields_from_schema(BOOLEAN_ENUM)
    assert values_from_form([field], {field.input_name: submitted}) == {"approved": False}


def test_an_enum_does_not_excuse_a_type_this_cannot_ask_for() -> None:
    """An enum used to downgrade any unreadable declared type to "string",
    which made it a hole in the whole-or-nothing rule.
    """
    assert fields_from_schema({"a": {"type": "object", "enum": [{"x": 1}]}}) == []
    assert fields_from_schema({"a": {"type": "array", "enum": [["x"], ["y"]]}}) == []
    # An enum with NO declared type is still strings -- that is what its
    # members are, and nothing has been bypassed.
    (field,) = fields_from_schema({"a": {"enum": ["x", "y"]}})
    assert field.value_type == "string"


def test_assignee_is_routing_metadata_and_never_a_question() -> None:
    """`runner._assignee` reads `human_schema.assignee` to decide who a step
    parks on. Rendering it as an input would invite the operator answering
    the step to overwrite the routing that put it in front of them.
    """
    fields = fields_from_schema({"assignee": "someone@example.com", "approved": "boolean"})
    assert _names(fields) == ["approved"]


@pytest.mark.parametrize(
    "schema",
    [
        pytest.param(None, id="absent"),
        pytest.param({}, id="empty"),
        pytest.param("approved", id="not-an-object"),
        pytest.param({"type": "object"}, id="object-with-no-properties"),
        pytest.param({"approved": {"type": "object"}}, id="nested-object"),
        pytest.param({"steps": {"type": "array", "items": {"type": "object"}}}, id="object-array"),
        pytest.param({"approved": 3}, id="property-that-is-not-a-type"),
        pytest.param({"assignee": "someone@example.com"}, id="routing-metadata-only"),
    ],
)
def test_a_shape_this_cannot_ask_for_produces_no_form_at_all(schema: Any) -> None:
    """The whole-or-nothing rule. See the module docstring.

    An empty list is not a failure -- it is the caller's signal to render the
    JSON textarea, which the templates then LABEL as the fallback rather than
    presenting as the only way in.
    """
    assert fields_from_schema(schema) == []


def test_one_unaskable_property_disqualifies_the_whole_schema() -> None:
    """Two askable properties and one that is not is still nothing: a form
    with the two would silently drop the third from every answer.
    """
    assert (
        fields_from_schema(
            {
                "type": "object",
                "properties": {
                    "approved": {"type": "boolean"},
                    "note": {"type": "string"},
                    "attachments": {"type": "object"},
                },
            }
        )
        == []
    )


# ---------------------------------------------------------------------------
# Reading a submission back
# ---------------------------------------------------------------------------

_MIXED = fields_from_schema(
    {
        "type": "object",
        "required": ["owner"],
        "properties": {
            "owner": {"type": "string"},
            "approved": {"type": "boolean"},
            "attempts": {"type": "integer"},
            "ratio": {"type": "number"},
            "tags": {"type": "array"},
        },
    }
)


def _submitted(**values: str) -> dict[str, str]:
    """A form as the browser posts it -- every generated control prefixed.

    Spelled out here rather than hidden in the helper's callers so these
    tests would fail if `values_from_form` stopped stripping the prefix,
    which is the half of the collision fix that lives in Python.
    """
    return {f"{FIELD_PREFIX}{name}": value for name, value in values.items()}


def test_a_filled_form_becomes_the_json_object_the_textarea_used_to_produce() -> None:
    assert values_from_form(
        _MIXED,
        _submitted(
            owner="deal desk",
            approved="1",
            attempts="2",
            ratio="0.25",
            tags="urgent, repriced\nescalated",
        ),
    ) == {
        "owner": "deal desk",
        "approved": True,
        "attempts": 2,
        "ratio": 0.25,
        "tags": ["urgent", "repriced", "escalated"],
    }


def test_an_unchecked_box_is_false_rather_than_missing() -> None:
    """HTML does not submit an unchecked checkbox at all, which is exactly
    how it says false -- so a boolean is always present in the result, and
    `$.approved == false` is a jsonpath a decision branch can rely on.
    """
    assert values_from_form(_MIXED, _submitted(owner="deal desk"))["approved"] is False


def test_a_blank_optional_field_is_omitted_rather_than_sent_as_empty() -> None:
    """`$.input.note` being absent and being "" are different things to a
    jsonpath, and only one of them means "not answered".
    """
    values = values_from_form(_MIXED, _submitted(owner="deal desk"))
    assert "attempts" not in values
    assert "tags" not in values


def test_an_integer_array_arrives_as_numbers_rather_than_strings() -> None:
    """`triage_drift` matches `signal_ids` against `drift_list` output, where
    a signal_id is a number. `['41']` and `[41]` are different questions.
    """
    (field,) = fields_from_schema({"ids": {"type": "array", "items": {"type": "integer"}}})
    assert values_from_form([field], _submitted(ids="41, 42")) == {"ids": [41, 42]}


def test_one_bad_entry_in_a_list_names_the_entry_not_the_list() -> None:
    (field,) = fields_from_schema({"ids": {"type": "array", "items": {"type": "integer"}}})
    with pytest.raises(GateError) as caught:
        values_from_form([field], _submitted(ids="41, forty-two"))
    assert caught.value.message == "every entry of Ids must be a whole number, not 'forty-two'."


def test_a_missing_required_field_is_refused_by_its_label() -> None:
    """Re-checked on the server, not left to the browser -- and named by the
    LABEL, because the operator never saw the key.
    """
    with pytest.raises(GateError) as caught:
        values_from_form(_MIXED, _submitted(approved="1"))
    assert caught.value.message == "Owner is required."


def test_a_number_field_given_prose_says_so_in_plain_language() -> None:
    with pytest.raises(GateError) as caught:
        values_from_form(_MIXED, _submitted(owner="deal desk", attempts="twice"))
    assert caught.value.message == "Attempts must be a whole number, not 'twice'."


# ---------------------------------------------------------------------------
# What the pages actually render
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("field", "expected"),
    [
        pytest.param(
            Field(name="slug", label="Short name", required=True, placeholder="q2c"),
            ' required placeholder="q2c"',
            id="text",
        ),
        pytest.param(
            Field(name="note", label="Note", rows=4, required=True, placeholder="why"),
            ' required placeholder="why"',
            id="textarea",
        ),
        pytest.param(
            Field(name="n", label="N", value_type="integer", required=True, placeholder="3"),
            ' required placeholder="3"',
            id="number",
        ),
    ],
)
def test_optional_attributes_never_run_together(field: Field, expected: str) -> None:
    """The environment uses `lstrip_blocks`, which eats the indentation in
    front of a `{% if %}`. One conditional attribute per line therefore
    rendered `requiredplaceholder="…"` -- a single unknown attribute, with
    the `required` silently gone. It shipped that way until someone read the
    HTML; this is what keeps it read.
    """
    macro = ui._ENV.get_template("_fields.html.j2").module
    markup = str(macro.field(field))  # type: ignore[attr-defined]
    assert expected in markup
    assert "requiredplaceholder" not in markup


_WORKFLOW = {"workflow_id": 7, "slug": "q2c", "version": 1, "title": "Quote to cash"}


def _runs_page(**overrides: Any) -> str:
    context: dict[str, Any] = {
        "principal": "sme@example.com",
        "is_admin": False,
        "error": None,
        "notice": None,
        "rendered_at": 0.0,
        "runs": [],
        "workflows": [_WORKFLOW],
        "status": None,
        "selected_workflow": _WORKFLOW,
        "input_schema_step": {"step_key": "approve", "title": "Approve it", "schema": SMOKE_SCHEMA},
        "input_fields": fields_from_schema(SMOKE_SCHEMA),
    }
    return ui.render("runs.html.j2", **{**context, **overrides})


def test_the_run_start_form_renders_fields_and_no_json_textarea() -> None:
    """The textarea this whole section exists to remove. Its `name="input"`
    is the thing to assert on -- the page still has textareas, generated
    from schemas, and only the JSON one is the regression.
    """
    page = _runs_page()
    assert 'name="f_approved"' in page
    assert 'name="input"' not in page
    # And it names the step it took the fields from, rather than presenting
    # a heuristic as a fact.
    assert "approve" in page


def test_a_workflow_declaring_nothing_gets_a_textarea_that_says_so() -> None:
    page = _runs_page(input_schema_step=None, input_fields=[])
    assert 'name="input"' in page
    assert "declares no input shape" in page


def test_choosing_no_workflow_shows_only_the_picker() -> None:
    page = _runs_page(selected_workflow=None, input_schema_step=None, input_fields=[])
    assert 'name="input"' not in page
    assert 'name="workflow_id"' in page


def _run_page(schema: Any) -> str:
    step = {
        "run_step_id": 3,
        "title": "Approve the discount",
        "instruction": "Deal desk decides.",
        "human_prompt": "Approve this discount?",
        "human_schema": schema,
        "input": {},
    }
    return ui.render(
        "run.html.j2",
        principal="sme@example.com",
        is_admin=False,
        error=None,
        notice=None,
        rendered_at=0.0,
        run={
            "run_id": 4,
            "workflow_title": "Quote to cash",
            "status": "awaiting_human",
            "started_by": "sme@example.com",
            "started_at": "now",
            "finished_at": None,
            "runtime_session_id": None,
            "error": None,
            "input": {},
            "context": {},
            "steps": [],
            "awaiting": [step],
        },
        answer_fields={3: fields_from_schema(schema)},
    )


def test_answering_a_human_step_is_a_form_when_the_step_declares_one() -> None:
    """`human_schema` is db/006's "the accepted answers" -- the one place in
    this console where a schema and a form are the same statement.
    """
    page = _run_page(SMOKE_SCHEMA)
    assert 'name="f_approved"' in page
    assert 'name="response"' not in page


def test_answering_a_step_that_declares_nothing_falls_back_and_says_so() -> None:
    page = _run_page(None)
    assert 'name="response"' in page
    assert "declares no answer shape" in page


def test_a_schema_that_cannot_become_fields_is_shown_rather_than_hidden() -> None:
    """The operator gets the textarea AND the shape they are meant to match.
    Silently dropping to a bare box would hide the only description of the
    answer that exists.
    """
    page = _run_page({"attachments": {"type": "object"}})
    assert 'name="response"' in page
    assert "cannot turn into fields" in page
    assert "attachments" in page


# ---------------------------------------------------------------------------
# The two things a schema must not be able to do to the page it renders on
# ---------------------------------------------------------------------------

#: A schema declaring exactly the names the forms use for their own controls.
#: `wf_draft` accepts any object as `human_schema`, so none of this needs a
#: hostile author -- "workflow_id" is an ordinary thing to call a field.
COLLIDING_SCHEMA: dict[str, Any] = {
    "workflow_id": "string",
    "run_id": "string",
    "action": "string",
}


def test_a_schema_cannot_name_a_control_the_form_already_has() -> None:
    """Form parsing keeps the LAST value for a repeated key
    (`http.py:parse_apigw_event`), and generated controls render after the
    hidden ones -- so an unprefixed `workflow_id` would have started a
    different workflow than the operator picked, `action` would have
    overridden approve/retry/skip, and `run_id` would have corrupted the
    redirect. The prefix is what keeps the two namespaces apart.
    """
    fields = fields_from_schema(COLLIDING_SCHEMA)
    assert _names(fields) == ["workflow_id", "run_id", "action"]
    for field in fields:
        assert field.input_name != field.name
        assert field.input_name == f"{FIELD_PREFIX}{field.name}"


def _post_form(page: str, action: str) -> str:
    """The one form that POSTs to `action`. Scoped because collisions are a
    property of a FORM, not of a page -- the runs page also has a separate
    GET picker whose select is legitimately named `workflow_id`.
    """
    start = page.index(f'<form method="post" action="{action}"')
    return page[start : page.index("</form>", start)]


def test_the_rendered_start_form_has_exactly_one_workflow_id_control() -> None:
    """The claim, in the markup. Two controls named `workflow_id` in one form
    is the whole bug; one is the fix.
    """
    page = _runs_page(input_fields=fields_from_schema(COLLIDING_SCHEMA))
    form = _post_form(page, "/ui/runs/start")
    assert form.count('name="workflow_id"') == 1, "the hidden control, and nothing else"
    assert 'name="f_workflow_id"' in form


def test_the_rendered_answer_form_has_exactly_one_action_control() -> None:
    form = _post_form(_run_page(COLLIDING_SCHEMA), "/ui/run-steps/3/respond")
    assert form.count('name="action"') == 1, "the approve/retry/skip select, and nothing else"
    assert form.count('name="run_id"') == 1, "the hidden control, and nothing else"
    assert 'name="f_action"' in form


def test_a_colliding_field_still_reaches_the_answer_under_its_own_name() -> None:
    """Prefixed on the wire, unprefixed in the result: the object handed to
    `wf.respond_human` is keyed by what the schema declared, not by an
    implementation detail of this console's forms.
    """
    fields = fields_from_schema(COLLIDING_SCHEMA)
    assert values_from_form(fields, _submitted(workflow_id="99", run_id="98", action="abort")) == {
        "workflow_id": "99",
        "run_id": "98",
        "action": "abort",
    }


# ---------------------------------------------------------------------------
# Escaping
# ---------------------------------------------------------------------------

#: Titles, descriptions and node labels are written by agents -- a
#: `human_schema` arrives through `wf_draft`, and option text on the launcher
#: comes from `kg.node.label`, which is merged proposal payload. The macro's
#: own comment names this as the untrusted path; this is that claim tested.
_MARKUP = '<script>alert("x")</script>'


def test_agent_authored_schema_text_renders_escaped() -> None:
    (field,) = fields_from_schema(
        {
            "type": "object",
            "properties": {"note": {"type": "string", "title": _MARKUP, "description": _MARKUP}},
        }
    )
    assert field.label == _MARKUP, "the value itself is carried verbatim"

    markup = str(ui._ENV.get_template("_fields.html.j2").module.field(field))  # type: ignore[attr-defined]
    assert "<script>" not in markup
    assert "&lt;script&gt;" in markup


def test_option_text_from_a_node_label_renders_escaped() -> None:
    """The launcher's process and source pickers label their options with
    `kg.node.label` and `kg.source.title` -- merged agent output, and a
    proposal payload containing markup is not hypothetical (it is a document
    extracted from a customer's SOP; see ui.py's module docstring).
    """
    field = Field(name="root_process_key", label="Process", options=((_MARKUP, _MARKUP),))
    markup = str(ui._ENV.get_template("_fields.html.j2").module.field(field))  # type: ignore[attr-defined]
    assert "<script>" not in markup
    assert markup.count("&lt;script&gt;") == 2, "escaped in the option's value AND its text"
