"""intake.py -- turning a pasted transcript into chunks the graph can see.

Three pure functions and no database. `service/sources.py` fetches the rows,
calls these, and writes the result; keeping the judgement here means the
chunker's boundary behaviour and the matcher's idea of "this passage mentions
that node" can be tested exhaustively without Postgres, and read without
holding a transaction in your head.

The chunker
-----------
`kg.chunk.content` is capped at 8000 characters by the embedder's budget
(`fde_mcp.ingest.CHUNK_CONTENT_MAX_CHARS`), and the naive way to honour that
is to slice every 8000 characters. That splits mid-sentence, and a chunk is
not a storage unit -- it is the passage a reviewer reads to check a claim and
the text an embedding has to mean something about. Half a sentence embeds to
half a meaning.

So the split follows the document's own structure and only falls back when
the structure is too coarse: paragraphs, then sentences within an oversized
paragraph, then a hard slice within a sentence longer than the cap (a
transcript exported without punctuation is a real thing, and refusing it
would be refusing the input this feature exists to accept). Every fallback is
strictly more aggressive than the last, so the cap always holds.

The anchor matcher, and what it deliberately is not
----------------------------------------------------
`kg.hybrid_search`'s chunk arm keeps only chunks with at least one anchor
(db/008_retrieval.sql:317), so anchoring is what decides whether ingested
text is reachable at all. The matcher here is lexical and conservative: a
chunk anchors to a live node when the node's label -- or its key with the
namespace segment stripped and the separators opened out,
`act.discount_review` -> "discount review" -- appears in the chunk as whole
words. Stripping that first segment is load-bearing, not cosmetic: `act`,
`sys` and `role` are the one part of a key that never appears in a
transcript, and leaving them in meant no key ever matched (see
`_readable_key`).

It reads the same relation `kg.lexical_search` reads (`kg.node_current`,
filtered by engagement) and deliberately does NOT reuse its ranking.
`kg.lexical_search` is trigram similarity between a short query and a short
label; a 6000-character chunk against a three-word label scores near zero on
that measure, because the measure is calibrated for the opposite shape of
comparison. Substring-with-word-boundaries is the honest test for "does this
passage mention this thing", and it has the property an operator needs: they
can look at the chunk, see the words, and agree. Nothing in db/008 is
modified, read or written, by any of this.

Conservative in both directions, and that is the point. It will miss a node
the transcript refers to by a synonym -- which is exactly what the
`ingest_interview` agent run is for, and why coverage is displayed rather
than assumed. It will not invent an anchor nobody can see in the text.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

from fde_mcp.ingest import CHUNK_CONTENT_MAX_CHARS

__all__ = [
    "MAX_TRANSCRIPT_CHARS",
    "MIN_ANCHOR_PHRASE_CHARS",
    "Coverage",
    "LiveNode",
    "MatchedChunk",
    "chunk_transcript",
    "coverage_of",
    "match_anchors",
    "split_into_chunks",
]

# An upper bound on one paste or upload. Not a database limit -- it is the
# point past which "one source" has stopped being one interview, and where the
# operator is better served by a refusal naming the fix than by a console that
# appears to hang. 1M characters is roughly 170,000 words: a very long day of
# interviews, and comfortably inside the 6MB Lambda request payload.
MAX_TRANSCRIPT_CHARS = 1_000_000

# A phrase shorter than this is not evidence of anything. A node labelled
# "AR" would otherwise anchor every chunk containing the word "are" once
# punctuation is stripped -- an anchor that is always true carries no
# information and quietly poisons the ranking for that node.
MIN_ANCHOR_PHRASE_CHARS = 4

# A paragraph break: a blank line, however much horizontal space is in it.
_PARAGRAPH_BREAK = re.compile(r"\n[ \t]*\n\s*")

# A sentence end: terminal punctuation followed by whitespace. Kept simple on
# purpose -- this only ever runs on a paragraph that has already exceeded the
# cap, where the alternative is a hard slice, so a split after "Dr." costs
# nothing worth a list of abbreviations to prevent.
# The full-width forms are deliberate, not typos for the ASCII ones beside
# them -- hence the RUF001 suppression. A transcript is in whatever language
# the interview was, and without them a CJK paragraph over the cap has no
# sentence boundaries at all and falls straight through to the hard slice.
_SENTENCE_END = re.compile(r"(?<=[.!?。！？])\s+")  # noqa: RUF001

# Everything that is not a letter, a digit, or a mark. Unicode-aware via
# str.isalnum() rather than \w, so accented and non-Latin scripts survive
# normalisation instead of becoming separators.
_SEPARATORS = re.compile(r"\s+")


@dataclass(frozen=True, slots=True)
class LiveNode:
    """One live graph node, as the matcher needs it.

    Exactly the two columns `kg.node_current` is read for. A dataclass rather
    than the raw row dict so the matcher cannot accidentally depend on a
    column the query might stop selecting.
    """

    node_key: str
    label: str


@dataclass(frozen=True, slots=True)
class MatchedChunk:
    """One chunk with the anchors the matcher found for it."""

    ordinal: int
    content: str
    anchor_keys: tuple[str, ...]

    @property
    def is_dark(self) -> bool:
        """True when retrieval will never surface this text.

        Named for what it means operationally rather than for the column it
        reads: `anchor_keys = {}` is a fact about a row, "dark" is the fact
        about the business -- the interview is in the database and the graph
        cannot see it.
        """
        return not self.anchor_keys


@dataclass(frozen=True, slots=True)
class Coverage:
    """How much of a source retrieval can actually reach."""

    total: int
    anchored: int

    @property
    def dark(self) -> int:
        return self.total - self.anchored

    @property
    def ratio(self) -> float:
        """Anchored fraction, 0.0 when there is nothing to cover.

        An empty source is reported as 0% rather than 100%: "everything we
        have is reachable" and "we have nothing" should not render as the
        same number on a page whose whole job is telling an operator whether
        their interview made it in.
        """
        return self.anchored / self.total if self.total else 0.0

    @property
    def percent(self) -> int:
        """The ratio as a whole number, for display.

        Truncated, never rounded up: 199 anchored chunks out of 200 must not
        render as "100%" on a page whose entire purpose is to be believed
        about what is missing.
        """
        return int(self.ratio * 100)


def split_into_chunks(text: str, *, max_chars: int = CHUNK_CONTENT_MAX_CHARS) -> list[str]:
    """Split a transcript into chunks of at most `max_chars` characters.

    Paragraph boundaries first, sentence boundaries inside a paragraph too
    long to fit, and a hard slice inside a sentence too long to fit. Returns
    `[]` for input that is empty or only whitespace -- a document with no
    text is zero chunks, not one empty one, and `kg.chunk.content` is NOT
    NULL for the same reason.

    Line endings are normalised (CRLF and CR to LF) before anything else, so
    the same transcript pasted from Windows, from a Mac, and from a browser
    produces identical chunks and therefore an identical checksum. That is
    what makes the dedupe in `kg_register_source` mean "the same document"
    rather than "the same bytes".
    """
    if max_chars < 1:
        raise ValueError(f"max_chars must be positive, got {max_chars}")

    normalised = text.replace("\r\n", "\n").replace("\r", "\n").strip()
    if not normalised:
        return []

    chunks: list[str] = []
    pending: list[str] = []
    pending_len = 0

    def flush() -> None:
        nonlocal pending, pending_len
        if pending:
            chunks.append("\n\n".join(pending))
            pending = []
            pending_len = 0

    for paragraph in _PARAGRAPH_BREAK.split(normalised):
        block = paragraph.strip()
        if not block:
            continue
        for piece in _fit(block, max_chars):
            # +2 for the "\n\n" that will join this piece to the last one.
            joined_len = pending_len + (2 if pending else 0) + len(piece)
            if pending and joined_len > max_chars:
                flush()
            pending.append(piece)
            pending_len = len(piece) if len(pending) == 1 else joined_len
    flush()
    return chunks


def _fit(block: str, max_chars: int) -> list[str]:
    """Break one paragraph down until every piece fits, escalating as needed."""
    if len(block) <= max_chars:
        return [block]

    pieces: list[str] = []
    buffer = ""
    for sentence in _SENTENCE_END.split(block):
        candidate = f"{buffer} {sentence}" if buffer else sentence
        if len(candidate) <= max_chars:
            buffer = candidate
            continue
        if buffer:
            pieces.append(buffer)
            buffer = ""
        if len(sentence) <= max_chars:
            buffer = sentence
        else:
            # One sentence longer than the whole budget. Slice it. Python
            # slices by code point, which is the unit the cap is expressed
            # in, so no character is ever split in half or lost here.
            for start in range(0, len(sentence), max_chars):
                slice_ = sentence[start : start + max_chars]
                if len(slice_) == max_chars:
                    pieces.append(slice_)
                else:
                    buffer = slice_
    if buffer:
        pieces.append(buffer)
    return pieces


def chunk_transcript(text: str, *, max_chars: int = CHUNK_CONTENT_MAX_CHARS) -> list[str]:
    """`split_into_chunks`, refusing input too large to be one source."""
    if len(text) > MAX_TRANSCRIPT_CHARS:
        raise ValueError(
            f"this document is {len(text):,} characters, over the "
            f"{MAX_TRANSCRIPT_CHARS:,}-character limit for a single source. "
            "Split it into separate sources -- one interview or one document "
            "each -- which is also how the graph will cite them."
        )
    return split_into_chunks(text, max_chars=max_chars)


def _normalise(text: str) -> str:
    """Casefold, strip accents' variability, and reduce to space-separated words.

    NFKC first so "ﬁ" and "fi" compare equal; then every character that is
    not alphanumeric becomes a space, which turns punctuation, hyphens and
    line breaks into word boundaries. The result is padded by the caller so
    " quote " cannot match inside "quotes".
    """
    folded = unicodedata.normalize("NFKC", text).casefold()
    return _SEPARATORS.sub(" ", "".join(c if c.isalnum() else " " for c in folded)).strip()


def _readable_key(node_key: str) -> str:
    """`act.discount_review` -> "discount review". The words people say.

    The leading dotted segment is a NAMESPACE -- `act`, `sys`, `role`, `proc`
    -- and it is the one part of a key that never appears in the transcript.
    Keeping it was a real bug: every key-derived phrase began with a token no
    interviewee utters, so no key ever matched and only labels anchored. The
    tests passed anyway, because the node they used had a label that happened
    to contain the same words.

    Only the FIRST segment goes. A deeper key keeps the rest of its structure,
    since `a.b.c` is a namespace and a two-word name, not two namespaces.
    """
    _, _, remainder = node_key.partition(".")
    return (remainder or node_key).replace(".", " ").replace("_", " ")


def _phrases(node: LiveNode) -> set[str]:
    """The forms of this node worth looking for in a transcript.

    Two: the human label an interviewee would say, and the key with its
    namespace stripped and its separators opened out into spaces, because a
    key is authored as a dotted slug and the words inside it are usually the
    words in the room. Both go through the same normalisation as the chunk, so
    the comparison is between two strings shaped the same way.
    """
    candidates = {node.label, _readable_key(node.node_key)}
    return {
        normalised
        for candidate in candidates
        if len(normalised := _normalise(candidate)) >= MIN_ANCHOR_PHRASE_CHARS
    }


def match_anchors(contents: list[str], nodes: list[LiveNode]) -> list[MatchedChunk]:
    """Anchor each chunk to every live node whose name it mentions.

    Ordinals are assigned here, 0-based and dense, because they are the
    chunk's position in the source and the only thing `ON CONFLICT
    (source_id, ordinal)` has to work with.

    Node keys come back sorted, so two runs over the same text and the same
    graph produce byte-identical rows -- which is what lets the re-ingest
    checksum below detect "nothing has changed" instead of writing a second
    identical source every time someone clicks the button.
    """
    prepared = [(node.node_key, _phrases(node)) for node in nodes]
    matched: list[MatchedChunk] = []
    for ordinal, content in enumerate(contents):
        haystack = f" {_normalise(content)} "
        keys = sorted(
            node_key
            for node_key, phrases in prepared
            if any(f" {phrase} " in haystack for phrase in phrases)
        )
        matched.append(MatchedChunk(ordinal=ordinal, content=content, anchor_keys=tuple(keys)))
    return matched


def coverage_of(chunks: list[MatchedChunk]) -> Coverage:
    """How many of these chunks retrieval will be able to see."""
    return Coverage(total=len(chunks), anchored=sum(1 for c in chunks if not c.is_dark))
