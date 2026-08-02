"""Tests for `fde_training.lambda_grader` and its packaging.

`lambda_grader.py`'s own docstring pointed at unit tests that did not
exist. These are them. The module is the reward the Bedrock RFT path
actually optimises against, and it is a SECOND implementation of scoring
rules `fde_training.rewards` also implements -- a duplication that is only
acceptable while both are pinned down, so the scoring rules are asserted
term by term rather than through `score()` alone.

`test_built_zip_imports_and_runs_in_isolation` is the one that keeps the
"stdlib only, this file zips up alone" contract honest: it imports the
handler out of the built archive with `sys.path` pointing at nothing else
of ours, so a stray `from fde_training import ...` fails here rather than
at the first RFT invocation.
"""

from __future__ import annotations

import json
import sys
import zipfile
from pathlib import Path
from typing import Any

import pytest

from fde_training import bedrock_rft, lambda_grader

PROVENANCE = ["ctl.discount_threshold_20", "act.discount_review", "role.deal_desk"]


# ===========================================================================
# _grounded_score
# ===========================================================================
def test_grounded_score_rewards_abstention_over_a_confident_guess() -> None:
    """An honest "I don't know" is worth more than a fluent uncited claim --
    the same priority `DEFAULT_WEIGHTS` encodes for the self-managed path."""
    assert lambda_grader._grounded_score("No information found.", PROVENANCE) == 1.0
    assert lambda_grader._grounded_score("Deal desk approves it.", PROVENANCE) == 0.0
    assert lambda_grader._grounded_score("", PROVENANCE) == 1.0


def test_grounded_score_is_the_fraction_of_citations_that_are_real() -> None:
    text = "See [ctl.discount_threshold_20] and [not.a.real.key]."
    assert lambda_grader._grounded_score(text, PROVENANCE) == 0.5
    assert lambda_grader._grounded_score("[act.discount_review]", PROVENANCE) == 1.0


# ===========================================================================
# _outcome_f1
# ===========================================================================
@pytest.mark.parametrize(
    ("text", "gold", "expected"),
    [
        ("nothing cited", [], 1.0),
        ("[a.one]", [], 0.0),
        ("nothing cited", ["a.one"], 0.0),
        ("[a.two]", ["a.one"], 0.0),
        ("[a.one] [a.two]", ["a.one", "a.two"], 1.0),
        ("[a.one]", ["a.one", "a.two"], 2 / 3),
    ],
)
def test_outcome_f1(text: str, gold: list[str], expected: float) -> None:
    assert lambda_grader._outcome_f1(text, gold) == pytest.approx(expected)


# ===========================================================================
# _citation_format_score
# ===========================================================================
def test_citation_format_score_exempts_short_and_empty_answers() -> None:
    """Short acknowledgements ("Yes.", "Correct.") are not claims needing a
    citation; scoring them 0 would train the model to pad them out."""
    assert lambda_grader._citation_format_score("") == 1.0
    assert lambda_grader._citation_format_score("Yes. No.") == 1.0


def test_citation_format_score_is_the_fraction_of_cited_substantive_sentences() -> None:
    text = (
        "Approval sits with the deal desk [role.deal_desk]. It is triggered above twenty percent."
    )
    assert lambda_grader._citation_format_score(text) == 0.5


# ===========================================================================
# score()
# ===========================================================================
def test_score_is_the_weighted_sum_of_its_three_components() -> None:
    text = "Approval sits with the deal desk [role.deal_desk]."
    result = lambda_grader.score(text, ["role.deal_desk"], PROVENANCE)
    components = result["components"]
    expected = sum(lambda_grader.WEIGHTS[k] * components[k] for k in lambda_grader.WEIGHTS)
    assert result["reward"] == pytest.approx(expected, abs=1e-6)
    assert 0.0 <= result["reward"] <= 1.0


def test_score_stays_in_range_for_a_worst_case_answer() -> None:
    result = lambda_grader.score("Everything is fine and nothing needs approval.", ["a.one"], [])
    assert 0.0 <= result["reward"] <= 1.0


# ===========================================================================
# _extract_response_and_reference
# ===========================================================================
def test_extract_prefers_model_response_and_reads_a_dict_content() -> None:
    text, gold, provenance = lambda_grader._extract_response_and_reference(
        {
            "modelResponse": {"content": "answer text"},
            "response": "should be ignored",
            "referenceResponse": {"gold_keys": ["a.one"], "provenance_keys": ["a.two"]},
        }
    )
    assert text == "answer text"
    assert gold == ["a.one"]
    assert provenance == ["a.two"]


def test_extract_parses_a_json_string_reference_and_tolerates_a_malformed_one() -> None:
    """RFT event shapes vary between accounts; a reference that arrives as a
    JSON string is parsed, and one that is not JSON degrades to empty
    rather than taking the whole grader down."""
    _, gold, _ = lambda_grader._extract_response_and_reference(
        {"completion": "x", "reference": json.dumps({"gold_keys": ["a.one"]})}
    )
    assert gold == ["a.one"]

    _, gold2, provenance2 = lambda_grader._extract_response_and_reference(
        {"completion": "x", "reference": "not json at all"}
    )
    assert gold2 == []
    assert provenance2 == []


# ===========================================================================
# lambda_handler
# ===========================================================================
def test_lambda_handler_returns_a_bounded_reward() -> None:
    response = lambda_grader.lambda_handler(
        {
            "modelResponse": "Approval sits with the deal desk [role.deal_desk].",
            "referenceResponse": {"gold_keys": ["role.deal_desk"], "provenance_keys": PROVENANCE},
        }
    )
    assert response["statusCode"] == 200
    body = json.loads(response["body"])
    assert 0.0 <= body["reward"] <= 1.0


def test_lambda_handler_never_raises_out_of_the_handler() -> None:
    """RFT reads the reward from a 200 body; an exception escaping here
    stalls the training job instead of scoring one bad rollout zero."""
    response = lambda_grader.lambda_handler({"modelResponse": object()})
    assert response["statusCode"] == 200
    body = json.loads(response["body"])
    assert body["reward"] == 0.0
    assert "error" in body


# ===========================================================================
# Packaging
# ===========================================================================
def test_build_grader_zip_contains_only_the_handler_module(tmp_path: Path) -> None:
    path = bedrock_rft.build_grader_zip(tmp_path / "grader.zip")
    with zipfile.ZipFile(path) as zf:
        assert zf.namelist() == ["lambda_grader.py"]


def test_build_grader_zip_is_deterministic(tmp_path: Path) -> None:
    """Two packaging runs of an unchanged grader must produce identical
    bytes, so a redeploy is a visible no-op rather than a new version
    nobody can account for."""
    first = bedrock_rft.build_grader_zip(tmp_path / "a.zip").read_bytes()
    second = bedrock_rft.build_grader_zip(tmp_path / "b.zip").read_bytes()
    assert first == second


def test_built_zip_imports_and_runs_in_isolation(tmp_path: Path) -> None:
    """The stdlib-only contract, enforced: import the handler from the
    archive alone and call it."""
    path = bedrock_rft.build_grader_zip(tmp_path / "grader.zip")
    sys.path.insert(0, str(path))
    for module in ("lambda_grader",):
        sys.modules.pop(module, None)
    try:
        import lambda_grader as packaged  # noqa: PLC0415

        assert packaged.__file__ is not None
        assert str(path) in packaged.__file__
        response = packaged.lambda_handler(
            {"modelResponse": "[role.deal_desk]", "referenceResponse": {"gold_keys": []}}
        )
        assert json.loads(response["body"])["reward"] >= 0.0
    finally:
        sys.path.remove(str(path))
        sys.modules.pop("lambda_grader", None)


def test_grader_function_config_targets_arm64_python312(tmp_path: Path) -> None:
    del tmp_path
    config: dict[str, Any] = bedrock_rft.build_grader_function_config(
        "fde-rft-grader", "arn:aws:iam::123456789012:role/grader"
    )
    assert config["Handler"] == bedrock_rft.GRADER_HANDLER
    assert config["Runtime"] == "python3.12"
    assert config["Architectures"] == ["arm64"]
    assert config["Timeout"] == 30
    assert config["FunctionName"] == "fde-rft-grader"
