"""Tests for the Development Agent's task surface.

`docs/04-agent-development.md` describes five tasks; four were implemented,
and the fifth (`review_agent`) fell through a bare `else` that rendered the
scaffold prompt instead -- so an unimplemented task would have produced a
confident, plausible, wrong response rather than an error. Both halves are
pinned here: the task exists, and an unknown one raises.

The prompt assertions check for the CONTRACT terms (PROVENANCE.md, the
autonomy ceiling, the read-only posture), not for prose. These strings are
model-facing tool descriptions in the sense `docs/11-python-conventions.md`
§6 means -- edited like prompts, not like comments -- so pinning whole
sentences would make every wording improvement a test failure.
"""

from __future__ import annotations

import pytest

from fde_agents.development import agent as development
from fde_agents.development.prompt import DEVELOPMENT_AGENT_SYSTEM_PROMPT


def _prompt(task: str, **task_input: object) -> str:
    return development._build_task_prompt(
        task, "eng-1", "sess-1", {"workflow_id": 42, **task_input}
    )


def test_review_agent_is_a_valid_task() -> None:
    assert "review_agent" in development.VALID_TASKS
    assert (
        frozenset(
            {
                "generate_agent_spec",
                "generate_evals",
                "generate_guardrails",
                "scaffold_agent",
                "review_agent",
            }
        )
        == development.VALID_TASKS
    )


@pytest.mark.parametrize("task", sorted(development.VALID_TASKS))
def test_every_valid_task_builds_a_prompt_naming_itself(task: str) -> None:
    """The bug this replaces: a task with no branch silently rendered the
    scaffold prompt, so the response looked right and answered the wrong
    question."""
    prompt = _prompt(task)
    assert f"TASK: {task}" in prompt
    assert "workflow_id: 42" in prompt
    assert "engagement_id='eng-1'" in prompt


def test_unknown_task_raises_rather_than_falling_through() -> None:
    with pytest.raises(ValueError, match="unknown development-agent task"):
        _prompt("generate_terraform")


def test_review_agent_prompt_asks_for_a_read_only_audit_against_the_package() -> None:
    prompt = _prompt(
        "review_agent", context={"files": {"agent.py": "print('hi')", "PROVENANCE.md": "# map"}}
    )
    assert "wf_get(workflow_id)" in prompt
    assert "PROVENANCE.md" in prompt
    assert "READ-ONLY" in prompt
    assert "agent.py" in prompt  # the package under review is actually passed in


def test_scaffold_prompt_requires_provenance_among_the_minimum_files() -> None:
    prompt = _prompt("scaffold_agent")
    for required in ("agent.py", "prompt.py", "requirements.txt", "Dockerfile", "PROVENANCE.md"):
        assert required in prompt


def test_system_prompt_has_a_review_section_with_the_audit_checklist() -> None:
    assert "# REVIEW_AGENT" in DEVELOPMENT_AGENT_SYSTEM_PROMPT
    for contract_term in (
        "PROVENANCE.md",
        "autonomy",
        "allowlist",
        "FINDINGS",
    ):
        assert contract_term in DEVELOPMENT_AGENT_SYSTEM_PROMPT


def test_scaffold_section_requires_a_provenance_map() -> None:
    scaffold_section = DEVELOPMENT_AGENT_SYSTEM_PROMPT.split("# GENERATE_SCAFFOLD_AGENT")[1]
    review_start = scaffold_section.index("# REVIEW_AGENT")
    assert "PROVENANCE.md" in scaffold_section[:review_start]


def test_review_agent_gains_no_extra_tools() -> None:
    """A reviewing agent that could write the graph would be able to make
    its own findings true."""
    assert (
        frozenset(
            {
                "kg_head_commit",
                "kg_as_of",
                "kg_search",
                "kg_lexical_search",
                "kg_get_node",
                "kg_traverse",
                "kg_dependency_closure",
                "kg_impact_radius",
                "kg_process_flow",
                "wf_list",
                "wf_get",
            }
        )
        == development._READ_ONLY_TOOL_NAMES
    )


def test_review_agent_produces_no_scaffold_result_event() -> None:
    """`_after_final_text`'s file-writing hook is scaffold-only; a review
    task must never write files, whatever it emitted in its findings."""
    events = development._after_final_text(
        "review_agent",
        {"write_to_disk": True},
        '```json\n{"files": {"agent.py": "x"}}\n```',
        False,
    )
    assert events == []
