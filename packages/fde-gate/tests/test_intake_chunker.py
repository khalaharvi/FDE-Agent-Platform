"""The chunker and the anchor matcher, with no database anywhere.

`fde_gate.intake` is pure on purpose (its header says why), so the properties
that actually matter can be asserted exhaustively rather than sampled through
a transaction:

* every chunk fits the 8000-character contract, whatever the input looks like;
* no character of the transcript is lost or duplicated on the way through;
* the matcher anchors on words a person can see in the passage, and does not
  anchor on fragments of other words.

The size cap is the one that would fail silently in production: a chunk one
character over is rejected by the pydantic schema on the MCP side and by
nothing at all on this side, so it would reach `kg.chunk.content` (unbounded
in the schema) and quietly exceed the embedder's budget.
"""

from __future__ import annotations

import pytest

from fde_gate.intake import (
    MAX_TRANSCRIPT_CHARS,
    LiveNode,
    chunk_transcript,
    coverage_of,
    match_anchors,
    split_into_chunks,
)
from fde_mcp.ingest import CHUNK_CONTENT_MAX_CHARS


def _visible(text: str) -> str:
    """The text with all whitespace removed.

    The comparison unit for "nothing was lost": the chunker deliberately
    normalises the whitespace BETWEEN pieces (paragraphs rejoin with a blank
    line, sentences with a single space), so comparing raw strings would fail
    on a difference that is the point of the function.
    """
    return "".join(text.split())


# ---------------------------------------------------------------------------
# The chunker
# ---------------------------------------------------------------------------


def test_short_transcript_is_one_chunk() -> None:
    chunks = split_into_chunks("Dana runs the quote desk.\n\nShe approves discounts.")
    assert len(chunks) == 1
    assert "quote desk" in chunks[0]


def test_empty_and_whitespace_only_input_produce_no_chunks() -> None:
    """Zero chunks, not one empty one: `kg.chunk.content` is NOT NULL."""
    assert split_into_chunks("") == []
    assert split_into_chunks("   \n\n \t \r\n ") == []


def test_line_endings_are_normalised_so_the_same_document_hashes_alike() -> None:
    """The dedupe means "the same document", not "the same bytes"."""
    unix = "Para one.\n\nPara two."
    windows = "Para one.\r\n\r\nPara two."
    old_mac = "Para one.\r\rPara two."
    assert split_into_chunks(unix) == split_into_chunks(windows) == split_into_chunks(old_mac)


@pytest.mark.parametrize(
    ("paragraph_count", "paragraph_size"),
    [(1, 200), (7, 900), (40, 2000), (3, 7999), (5, 8000), (2, 8001), (1, 24001)],
)
def test_every_chunk_fits_the_contract(paragraph_count: int, paragraph_size: int) -> None:
    """The cap holds for input the paragraph split cannot help with."""
    # No sentence punctuation: this forces the hard-slice fallback for the
    # oversized cases, which is the path most likely to overshoot.
    paragraph = "word " * (paragraph_size // 5)
    text = "\n\n".join(paragraph.strip() for _ in range(paragraph_count))

    chunks = split_into_chunks(text)

    assert chunks, "non-empty input must produce at least one chunk"
    assert all(len(c) <= CHUNK_CONTENT_MAX_CHARS for c in chunks)
    assert all(c.strip() for c in chunks), "no chunk may be empty or blank"
    assert _visible("".join(chunks)) == _visible(text)


def test_a_single_sentence_longer_than_the_cap_is_sliced_not_dropped() -> None:
    """A transcript exported without punctuation is a real thing."""
    text = "x" * (CHUNK_CONTENT_MAX_CHARS * 2 + 137)
    chunks = split_into_chunks(text)

    assert len(chunks) == 3
    assert [len(c) for c in chunks] == [
        CHUNK_CONTENT_MAX_CHARS,
        CHUNK_CONTENT_MAX_CHARS,
        137,
    ]
    assert "".join(chunks) == text


def test_an_oversized_paragraph_splits_on_sentences_before_slicing() -> None:
    """Sentence boundaries are preferred, so a chunk is a readable passage."""
    sentence = "The quote desk reviews every discount over ten percent. "
    text = sentence * 300  # comfortably over the cap

    chunks = split_into_chunks(text)

    assert all(len(c) <= CHUNK_CONTENT_MAX_CHARS for c in chunks)
    # Every chunk ends at a sentence end, which a hard slice would not do.
    assert all(c.rstrip().endswith(".") for c in chunks)
    assert _visible("".join(chunks)) == _visible(text)


def test_paragraphs_are_packed_rather_than_one_chunk_each() -> None:
    """Short paragraphs share a chunk; 200 one-liners must not be 200 chunks."""
    text = "\n\n".join(f"Speaker {i}: a short remark." for i in range(200))
    chunks = split_into_chunks(text)

    assert len(chunks) < 20
    assert all(len(c) <= CHUNK_CONTENT_MAX_CHARS for c in chunks)
    assert _visible("".join(chunks)) == _visible(text)


@pytest.mark.parametrize(
    "sample",
    [
        "Emoji survive: 👩‍💻 the analyst said 🎯. Then more.",
        "Accents: café naïveté Ångström. Wörter mit Umlauten.",
        "日本語の文章です。これは二番目の文です。三番目もあります。",
        "עברית מימין לשמאל. וגם משפט שני.",
        "Mixed ligature ﬁle and ASCII file in one line.",
    ],
)
def test_unicode_survives_chunking_unchanged(sample: str) -> None:
    """Characters are code points here, and none of them are mangled."""
    text = "\n\n".join([sample] * 40)
    chunks = split_into_chunks(text)

    assert all(len(c) <= CHUNK_CONTENT_MAX_CHARS for c in chunks)
    assert _visible("".join(chunks)) == _visible(text)


def test_unicode_is_not_split_mid_character_by_the_hard_slice() -> None:
    """Astral-plane characters count as one, and cannot be halved."""
    text = "👩‍💻" * 6000  # far over the cap, no whitespace to split on
    chunks = split_into_chunks(text)

    assert all(len(c) <= CHUNK_CONTENT_MAX_CHARS for c in chunks)
    assert "".join(chunks) == text
    assert "�" not in "".join(chunks)


def test_a_smaller_cap_is_honoured_too() -> None:
    """The cap is a parameter, so the property is about the parameter."""
    text = "\n\n".join(f"Sentence number {i} is here." for i in range(60))
    for cap in (30, 64, 200):
        chunks = split_into_chunks(text, max_chars=cap)
        assert all(len(c) <= cap for c in chunks), f"cap {cap}"
        assert _visible("".join(chunks)) == _visible(text)


def test_a_transcript_over_the_document_limit_is_refused_by_name() -> None:
    """The refusal names the size and the fix, not just "too large"."""
    with pytest.raises(ValueError, match="over the") as excinfo:
        chunk_transcript("x" * (MAX_TRANSCRIPT_CHARS + 1))
    message = str(excinfo.value)
    assert f"{MAX_TRANSCRIPT_CHARS:,}" in message
    assert "Split it into separate sources" in message


# ---------------------------------------------------------------------------
# The anchor matcher
# ---------------------------------------------------------------------------

NODES = [
    LiveNode(node_key="act.discount_review", label="Discount review"),
    LiveNode(node_key="sys.cpq", label="CPQ"),
    LiveNode(node_key="role.deal_desk", label="Deal desk analyst"),
]


def test_a_chunk_anchors_to_a_label_it_mentions() -> None:
    (chunk,) = match_anchors(["Every quote goes through discount review first."], NODES)
    assert chunk.anchor_keys == ("act.discount_review",)
    assert not chunk.is_dark


def test_matching_ignores_case_and_punctuation() -> None:
    (chunk,) = match_anchors(["...the DISCOUNT-REVIEW, then sign-off."], NODES)
    assert chunk.anchor_keys == ("act.discount_review",)


def test_the_node_key_matches_even_when_the_label_does_not() -> None:
    """Keys are dotted slugs; their words are how people say them out loud.

    Kept, but note it does NOT isolate the key path: "deal desk analyst" is
    also this node's label, so it passed even while key matching was broken.
    The test below is the one that actually holds the key path up.
    """
    (chunk,) = match_anchors(["Ask the deal desk analyst about it."], NODES)
    assert chunk.anchor_keys == ("role.deal_desk",)


def test_a_key_anchors_on_its_own_words_with_no_help_from_the_label() -> None:
    """The key path, isolated: the label cannot possibly match this text.

    This is the test that was missing. `_readable_key` has to drop the
    namespace segment -- `role`, `act`, `sys` are the one part of a key that
    never comes out of an interviewee's mouth -- and without the strip every
    key-derived phrase began with a token no transcript contains, so no key
    ever matched and coverage silently depended on labels alone.
    """
    nodes = [LiveNode(node_key="role.deal_desk", label="Revenue Operations Analyst II")]
    (chunk,) = match_anchors(["Ask the deal desk before promising a date."], nodes)
    assert chunk.anchor_keys == ("role.deal_desk",)


def test_the_namespace_segment_alone_does_not_anchor() -> None:
    """Stripping it must not leave it matchable by the back door."""
    nodes = [LiveNode(node_key="activity.discount_review", label="ZZZ Unrelated Label")]
    (prose,) = match_anchors(["The activity was scheduled for Tuesday."], nodes)
    assert prose.anchor_keys == ()

    (real,) = match_anchors(["It goes to discount review first."], nodes)
    assert real.anchor_keys == ("activity.discount_review",)


def test_a_key_with_no_namespace_keeps_all_of_its_words() -> None:
    """`partition` on a missing dot must not eat the whole key."""
    nodes = [LiveNode(node_key="quote_to_cash", label="ZZZ Unrelated Label")]
    (chunk,) = match_anchors(["The quote to cash process is the one we mean."], nodes)
    assert chunk.anchor_keys == ("quote_to_cash",)


def test_a_deep_key_keeps_everything_after_the_first_segment() -> None:
    """`a.b.c` is a namespace and a two-word name, not two namespaces."""
    nodes = [LiveNode(node_key="proc.order.fulfilment", label="ZZZ Unrelated Label")]
    (chunk,) = match_anchors(["We walked through order fulfilment end to end."], nodes)
    assert chunk.anchor_keys == ("proc.order.fulfilment",)


def test_a_one_word_key_tail_does_not_anchor_on_that_word() -> None:
    """`act.review` leaves "review", which is in a transcript about anything.

    A key tail is a slug, and a one-word slug is a category rather than a
    name. An anchor that is true of every chunk tells retrieval nothing and
    outranks the anchors that mean something, so key-derived phrases have to
    be more than one word. The LABEL is untouched by this -- see below.
    """
    nodes = [LiveNode(node_key="act.review", label="ZZZ Unrelated Label")]
    (chunk,) = match_anchors(["We review every deal before it ships."], nodes)
    assert chunk.anchor_keys == ()
    assert chunk.is_dark


def test_a_one_word_label_still_anchors() -> None:
    """The floor is on key-DERIVED phrases only. "Dunning" is what somebody
    chose to call this thing, and one word is a perfectly good name.
    """
    nodes = [LiveNode(node_key="proc.dunning", label="Dunning")]
    (chunk,) = match_anchors(["Dunning is handled by the collections team."], nodes)
    assert chunk.anchor_keys == ("proc.dunning",)


def test_a_multi_word_key_tail_is_unaffected_by_the_floor() -> None:
    """The case the key path exists for. Two words are a name, not a category,
    and this is the anchor that survives when the label does not match.
    """
    nodes = [LiveNode(node_key="act.discount_review", label="ZZZ Unrelated Label")]
    (chunk,) = match_anchors(["Everything over ten percent hits discount review."], nodes)
    assert chunk.anchor_keys == ("act.discount_review",)


def test_a_chunk_mentioning_nothing_is_dark() -> None:
    (chunk,) = match_anchors(["We broke for lunch at one o'clock."], NODES)
    assert chunk.anchor_keys == ()
    assert chunk.is_dark


def test_short_labels_do_not_anchor_on_fragments_of_other_words() -> None:
    """ "CPQ" is 3 characters, under the floor, so it anchors nothing.

    The floor exists because a two- or three-letter label matches inside
    ordinary words once punctuation is stripped, and an anchor that is always
    true carries no information at all.
    """
    (chunk,) = match_anchors(["The CPQ system is slow."], NODES)
    assert "sys.cpq" not in chunk.anchor_keys


def test_a_label_inside_a_longer_word_does_not_anchor() -> None:
    """Word boundaries, not substrings: "quote" must not match "quotes"."""
    nodes = [LiveNode(node_key="doc.quote", label="quote")]
    (inside,) = match_anchors(["He mentioned misquoted figures."], nodes)
    assert inside.anchor_keys == ()

    (boundary,) = match_anchors(["He sent the quote yesterday."], nodes)
    assert boundary.anchor_keys == ("doc.quote",)


def test_anchors_are_sorted_and_deduplicated() -> None:
    """Determinism: the same text and graph must produce the same rows.

    This is what lets the re-ingest checksum mean "nothing has changed"
    rather than "we hashed the anchors in a different order this time".
    """
    nodes = [
        LiveNode(node_key="z.last", label="Zeta process"),
        LiveNode(node_key="a.first", label="Alpha process"),
    ]
    text = "Alpha process feeds Zeta process, and Alpha process again."
    (chunk,) = match_anchors([text], nodes)
    assert chunk.anchor_keys == ("a.first", "z.last")


def test_ordinals_are_dense_and_zero_based() -> None:
    chunks = match_anchors(["one", "two", "three"], NODES)
    assert [c.ordinal for c in chunks] == [0, 1, 2]


def test_matching_against_no_live_nodes_leaves_everything_dark() -> None:
    """The ordinary state of a first interview, before anything is extracted."""
    chunks = match_anchors(["Anything at all.", "And more."], [])
    assert all(c.is_dark for c in chunks)
    assert coverage_of(chunks).percent == 0


# ---------------------------------------------------------------------------
# Coverage
# ---------------------------------------------------------------------------


def test_coverage_counts_and_percentages() -> None:
    chunks = match_anchors(
        ["Discount review happens here.", "Unrelated chatter.", "More discount review."],
        NODES,
    )
    coverage = coverage_of(chunks)
    assert (coverage.total, coverage.anchored, coverage.dark) == (3, 2, 1)
    assert coverage.percent == 66


def test_coverage_of_nothing_is_zero_not_one() -> None:
    """ "We have nothing" must not render as "everything is reachable"."""
    coverage = coverage_of([])
    assert coverage.ratio == 0.0
    assert coverage.percent == 0


def test_coverage_truncates_rather_than_rounding_up() -> None:
    """199 of 200 is not 100% on a page whose job is naming what is missing."""
    chunks = match_anchors(
        ["Discount review."] * 199 + ["nothing here"],
        NODES,
    )
    coverage = coverage_of(chunks)
    assert coverage.dark == 1
    assert coverage.percent == 99
