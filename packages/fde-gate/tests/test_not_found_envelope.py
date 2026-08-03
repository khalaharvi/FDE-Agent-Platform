"""Telling "there is no such thing" apart from "here is the thing".

Every read in `service/` that can find nothing returns `{"error": "..."}`,
and for a long time every caller tested `"error" in result` to decide whether
to answer 404. That idiom reads a MESSAGE key out of a result whose other
keys are COLUMN names, and nothing separates the two namespaces:
`wf.run.error` collided with it, so `/ui/runs/{id}` and `GET /api/runs/{id}`
answered "not found" for every run that existed until someone noticed.

`service.is_missing(result, key)` states the discrimination the other way
round -- on a key the success shape always carries. These tests hold up both
halves: the helper itself against a result carrying a domain column called
`error`, and each caller's chosen key against what its service function
actually returns, so a renamed key fails here rather than as a page that
404s in production.
"""

from __future__ import annotations

from typing import Any

import pytest
from gate_seed import SME

from fde_gate.service import drift, is_missing, proposals, runs, sources, workflows

# The id no fixture will ever mint, for the not-found half of each pair.
ABSENT = 10**9

requires_db = pytest.mark.requires_db


# ---------------------------------------------------------------------------
# The helper itself. No database: the collision is a fact about dict keys.
# ---------------------------------------------------------------------------


def test_a_domain_column_named_error_is_not_a_not_found_envelope() -> None:
    """The bug that produced this helper, as one assertion.

    `wf.run` has an `error` column, so a real run's result carries an `error`
    key -- usually null, and null is not absent. The old idiom saw the key and
    concluded the run did not exist.
    """
    found = {"run_id": 12, "error": None, "status": "running"}
    assert not is_missing(found, "run_id")
    assert "error" in found, "the column is still selected; that was never the bug"
    # The two answers, side by side. They disagree, and the helper is right.
    assert ("error" in found) is not is_missing(found, "run_id")


def test_the_not_found_envelope_is_missing_whatever_key_is_asked_for() -> None:
    envelope = {"error": "run 999 not found"}
    for key in ("run_id", "proposal_id", "workflow", "source", "signal"):
        assert is_missing(envelope, key)


def test_a_result_carrying_an_error_message_of_its_own_is_still_the_thing() -> None:
    """A failed run's `error` column holds a message, not None -- which is why
    "the column is usually null" was never a defence.
    """
    assert not is_missing({"run_id": 12, "error": "step 3 timed out"}, "run_id")


# ---------------------------------------------------------------------------
# The keys the callers pass, against the shapes they are given
# ---------------------------------------------------------------------------


@requires_db
async def test_a_proposal_that_exists_is_not_reported_as_missing(make_proposal: Any) -> None:
    created = make_proposal()
    assert not is_missing(await proposals.get_proposal(created["proposal_id"], SME), "proposal_id")
    assert is_missing(await proposals.get_proposal(ABSENT, SME), "proposal_id")


@requires_db
async def test_a_workflow_that_exists_is_not_reported_as_missing(make_workflow: Any) -> None:
    workflow_id = make_workflow([{"step_key": "do", "kind": "tool", "tool_name": "kg_search"}])

    assert not is_missing(await workflows.get_workflow(workflow_id), "workflow_id")
    assert is_missing(await workflows.get_workflow(ABSENT), "workflow_id")

    # The playbook is the OTHER shape: a wrapper, not the row itself, so its
    # callers cannot test the same key. Both of them -- the console page and
    # `playbook.md` -- test "workflow", which is the key they then read.
    playbook = await workflows.get_playbook(workflow_id)
    assert not is_missing(playbook, "workflow")
    assert playbook["workflow"]["workflow_id"] == workflow_id
    assert is_missing(await workflows.get_playbook(ABSENT), "workflow")


@requires_db
async def test_a_source_that_exists_is_not_reported_as_missing(seed: dict[str, Any]) -> None:
    assert not is_missing(await sources.get_source(SME, seed["source_id"]), "source")
    assert is_missing(await sources.get_source(SME, ABSENT), "source")


@requires_db
async def test_a_triaged_signal_is_not_reported_as_missing(seed: dict[str, Any], sql: Any) -> None:
    (signal,) = sql(
        """
        INSERT INTO sor.drift_signal (engagement_id, drift_kind, severity, subject_kind,
                                      subject_ref, detail)
        VALUES (%(eng)s, 'missing_in_sor', 'low', 'node', 'act.discount_review',
                '{"note": "pytest fixture"}'::jsonb)
        RETURNING signal_id
        """,
        {"eng": seed["engagement_id"]},
    )
    triaged = await drift.triage(signal["signal_id"], SME, "triaged", note="looked at it")
    assert not is_missing(triaged, "signal")
    assert is_missing(await drift.triage(ABSENT, SME, "triaged"), "signal")


@requires_db
async def test_the_run_reads_still_agree_with_the_shared_helper(make_workflow: Any) -> None:
    """`runs.is_missing` is the one-argument version of the same test, kept
    because the key that decides it belongs beside the query that returns it.
    It must not drift from the helper it delegates to.
    """
    workflow_id = make_workflow([{"step_key": "do", "kind": "tool", "tool_name": "kg_search"}])
    run_id = int((await runs.start_run(workflow_id, SME))["run"]["run_id"])

    found = await runs.get_run(run_id)
    assert runs.is_missing(found) is is_missing(found, "run_id")
    assert not runs.is_missing(found)

    absent = await runs.get_run(ABSENT)
    assert runs.is_missing(absent) is is_missing(absent, "run_id")
    assert runs.is_missing(absent)
