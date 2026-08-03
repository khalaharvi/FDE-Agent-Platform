"""Transcript intake against a live database: what gets written, and who may.

Three separate claims, tested separately because they fail separately:

* **Authorisation** -- any ACTIVE reviewer may register evidence, and nobody
  else. Deliberately a weaker bar than the roster page's admin check, so the
  test asserts BOTH halves: a reviewer holding no gate authority is allowed,
  and a deactivated one is not.
* **Semantics** -- the console's writes behave exactly like the MCP tools':
  checksum dedupe, immutable chunks, batching.
* **The guardrail's claim** -- `test_anchored_text_is_reachable_and_dark_text
  _is_not` is the whole feature's reason to exist, stated as a test:
  two chunks, the same embedding, and only the anchored one can influence a
  search. If that test ever passes for the dark chunk too, the coverage
  percentage on the console is measuring nothing.

Sources are never deleted (nothing holds DELETE on `kg.source`), so every
test here works inside its own engagement uuid and leaves its rows behind
rather than trying to clean up.
"""

from __future__ import annotations

import uuid
from http import HTTPStatus
from typing import Any
from urllib.parse import unquote

import psycopg
import pytest
from gate_seed import SME, STRANGER

from fde_gate import ui
from fde_gate.http import GateError, Request, Upload
from fde_gate.service import sources

pytestmark = pytest.mark.requires_db

# A transcript whose first paragraph names a node the graph will have, and
# whose second names nothing -- the ordinary shape of a real interview, and
# the reason coverage is a percentage rather than a yes/no.
#
# Each paragraph is padded past half the 8000-character cap so the chunker
# CANNOT pack them into one chunk. That padding is load-bearing, not
# decoration: the packing is correct behaviour (a chunk is the retrieval
# unit, and two sentences that fit belong together), but it would make one
# anchored sentence light up the whole document and leave these tests unable
# to say anything about dark passages at all.
_ANCHORED_FILLER = " The remark was transcribed verbatim from the recording." * 80
_DARK_FILLER = " Nothing of consequence was decided in this stretch of the call." * 80
ANCHORED_PARAGRAPH = (
    "Every deal over ten percent goes to discount review before it ships." + _ANCHORED_FILLER
)
DARK_PARAGRAPH = "We broke for coffee and chatted about the weather." + _DARK_FILLER
TRANSCRIPT = f"{ANCHORED_PARAGRAPH}\n\n{DARK_PARAGRAPH}"

# The node the first paragraph will anchor to, once it exists.
ANCHOR_KEY = "act.discount_review"
ANCHOR_LABEL = "Discount review"

# A node no paragraph of TRANSCRIPT mentions, used to prove that merging just
# any node does not retroactively light up a dark passage.
UNRELATED_KEY = "act.invoice_dispatch"
UNRELATED_LABEL = "Invoice dispatch"


@pytest.fixture
def engagement(seed: dict[str, Any], sql: Any) -> str:
    """A fresh engagement with a sealed commit and no nodes yet.

    No nodes on purpose: an interview arrives before the graph knows what it
    is about, and that ordering is the situation this whole feature exists
    to handle.
    """
    engagement_id = str(uuid.uuid4())
    sql(
        """
        INSERT INTO kg.commit (engagement_id, status, title, authored_by, sealed_by,
                               sealed_at, content_digest)
        VALUES (%(eng)s, 'sealed', 'intake fixture', 'pytest', 'pytest', now(),
                encode(digest('intake fixture', 'sha256'), 'hex'))
        """,
        {"eng": engagement_id},
    )
    return engagement_id


@pytest.fixture
def add_node(engagement: str, sql: Any) -> Any:
    """Merge a node into the fixture engagement, as the graph would have."""

    def _add(node_key: str, label: str) -> None:
        sql(
            """
            INSERT INTO kg.node (engagement_id, node_key, node_type, label, summary, commit_id)
            SELECT %(eng)s, %(key)s, 'activity', %(label)s, 'seeded by pytest', c.commit_id
              FROM kg.commit c
             WHERE c.engagement_id = %(eng)s
             ORDER BY c.commit_id LIMIT 1
            """,
            {"eng": engagement, "key": node_key, "label": label},
        )

    return _add


@pytest.fixture
def deactivated_reviewer(seed: dict[str, Any], sql: Any) -> str:
    """A reviewer who exists and is switched off."""
    principal = f"pytest-off-{uuid.uuid4().hex[:8]}@example.com"
    sql(
        """
        INSERT INTO hitl.reviewer (principal, display_name, is_active)
        VALUES (%(p)s, 'Deactivated Reviewer', false)
        """,
        {"p": principal},
    )
    return principal


async def _register(actor: str, engagement_id: str, text: str = TRANSCRIPT, **kw: Any) -> Any:
    return await sources.register(
        actor,
        engagement_id=engagement_id,
        title=kw.pop("title", "RevOps discovery interview"),
        source_kind=kw.pop("source_kind", "interview"),
        captured_at=kw.pop("captured_at", "2026-08-01"),
        text=text,
        **kw,
    )


# ---------------------------------------------------------------------------
# Authorisation
# ---------------------------------------------------------------------------


async def test_any_active_reviewer_may_register_evidence(engagement: str) -> None:
    """STRANGER holds no gate authority anywhere and is still allowed.

    This is the design decision, asserted rather than described: intake is
    gated on being a known, active person, not on being able to DECIDE
    anything. An interview that needs an administrator to upload it is an
    interview that stays in somebody's inbox.
    """
    result = await _register(STRANGER, engagement)
    assert result["created"] is True
    assert result["chunks"] == 2


async def test_an_unknown_principal_cannot_register_evidence(engagement: str) -> None:
    with pytest.raises(GateError) as excinfo:
        await _register("nobody@example.com", engagement)
    assert excinfo.value.status == HTTPStatus.FORBIDDEN
    assert "not an active reviewer" in excinfo.value.message
    assert "/ui/reviewers" in excinfo.value.message


async def test_an_anonymous_caller_cannot_register_evidence(engagement: str) -> None:
    with pytest.raises(GateError) as excinfo:
        await _register("", engagement)
    assert excinfo.value.status == HTTPStatus.FORBIDDEN


async def test_a_deactivated_reviewer_cannot_register_evidence(
    engagement: str, deactivated_reviewer: str
) -> None:
    """Deactivation has to mean this too, or it does not mean what it says."""
    with pytest.raises(GateError) as excinfo:
        await _register(deactivated_reviewer, engagement)
    assert excinfo.value.status == HTTPStatus.FORBIDDEN


async def test_the_console_refuses_a_stranger_with_a_page_not_an_envelope(
    engagement: str,
) -> None:
    """A browser gets HTML; the JSON envelope is for the API."""
    response = await ui.sources_page(
        Request(method="GET", path="/ui/sources", principal="nobody@example.com")
    )
    assert response.status == HTTPStatus.FORBIDDEN
    assert response.content_type.startswith("text/html")
    assert isinstance(response.body, str)
    assert "not an active reviewer" in response.body


async def test_listing_and_detail_also_require_an_active_reviewer(
    engagement: str,
) -> None:
    created = await _register(SME, engagement)
    for coroutine in (
        sources.list_sources("nobody@example.com"),
        sources.get_source("nobody@example.com", created["source_id"]),
        sources.reingest_dark_chunks("nobody@example.com", created["source_id"]),
    ):
        with pytest.raises(GateError) as excinfo:
            await coroutine
        assert excinfo.value.status == HTTPStatus.FORBIDDEN


# ---------------------------------------------------------------------------
# What gets written
# ---------------------------------------------------------------------------


async def test_registering_chunks_the_text_and_anchors_what_it_can(
    engagement: str, add_node: Any, sql: Any
) -> None:
    add_node(ANCHOR_KEY, ANCHOR_LABEL)
    result = await _register(SME, engagement)

    assert result["created"] is True
    assert result["chunks"] == 2
    assert result["enqueued"] == 2, "every chunk must reach the embedder queue"
    coverage = result["coverage"]
    assert (coverage.total, coverage.anchored, coverage.dark) == (2, 1, 1)
    assert coverage.percent == 50

    rows = sql(
        "SELECT ordinal, content, anchor_keys FROM kg.chunk WHERE source_id = %(s)s "
        "ORDER BY ordinal",
        {"s": result["source_id"]},
    )
    assert [r["anchor_keys"] for r in rows] == [[ANCHOR_KEY], []]
    assert ANCHORED_PARAGRAPH in rows[0]["content"]
    assert DARK_PARAGRAPH in rows[1]["content"]

    queued = sql(
        "SELECT count(*) AS n FROM kg.embed_queue WHERE subject_kind = 'chunk' "
        "AND subject_id IN (SELECT chunk_id FROM kg.chunk WHERE source_id = %(s)s)",
        {"s": result["source_id"]},
    )
    assert queued[0]["n"] == 2


async def test_the_source_row_records_who_and_when(engagement: str, sql: Any) -> None:
    result = await _register(SME, engagement, captured_at="2026-03-14")
    (row,) = sql(
        "SELECT captured_by, captured_at::date::text AS day, uri, metadata, checksum "
        "FROM kg.source WHERE source_id = %(s)s",
        {"s": result["source_id"]},
    )
    assert row["captured_by"] == SME
    # The day the interview happened, not the day it was typed up.
    assert row["day"] == "2026-03-14"
    assert row["uri"] is None, "pasted text has no artifact to point at"
    assert row["metadata"]["intake"]["captured_from"] == "paste"
    assert row["metadata"]["intake"]["version"] == 1
    assert len(row["checksum"]) == 64


async def test_the_same_transcript_twice_is_one_source(engagement: str) -> None:
    """The checksum dedupe the MCP tool has, on the console's path too."""
    first = await _register(SME, engagement)
    second = await _register(SME, engagement, title="a different title entirely")

    assert second["created"] is False
    assert second["source_id"] == first["source_id"]


async def test_line_ending_differences_do_not_create_a_second_source(
    engagement: str,
) -> None:
    """ "The same document" survives a trip through a different operating system."""
    first = await _register(SME, engagement)
    second = await _register(SME, engagement, text=TRANSCRIPT.replace("\n", "\r\n"))
    assert second["created"] is False
    assert second["source_id"] == first["source_id"]


async def test_the_same_transcript_on_another_engagement_is_its_own_source(
    engagement: str, sql: Any
) -> None:
    """The dedupe is scoped: one SOP genuinely used by two clients is two sources."""
    other = str(uuid.uuid4())
    sql(
        """
        INSERT INTO kg.commit (engagement_id, status, title, authored_by, sealed_by, sealed_at)
        VALUES (%(eng)s, 'sealed', 'other', 'pytest', 'pytest', now())
        """,
        {"eng": other},
    )
    first = await _register(SME, engagement)
    second = await _register(SME, other)
    assert second["created"] is True
    assert second["source_id"] != first["source_id"]


async def test_an_empty_transcript_is_refused_by_name(engagement: str) -> None:
    with pytest.raises(GateError) as excinfo:
        await _register(SME, engagement, text="   \n\n  ")
    assert excinfo.value.status == HTTPStatus.BAD_REQUEST
    assert "no text to ingest" in excinfo.value.message


async def test_a_bad_source_kind_names_the_valid_ones(engagement: str) -> None:
    with pytest.raises(GateError) as excinfo:
        await _register(SME, engagement, source_kind="podcast")
    assert "sop_document" in excinfo.value.message


async def test_a_bad_capture_date_says_what_the_field_is_for(engagement: str) -> None:
    with pytest.raises(GateError) as excinfo:
        await _register(SME, engagement, captured_at="last tuesday")
    assert "YYYY-MM-DD" in excinfo.value.message


async def test_a_transcript_over_the_batch_size_is_written_in_batches(
    engagement: str, sql: Any
) -> None:
    """250 chunks is two INSERT statements, not a refusal."""
    text = "\n\n".join(f"Paragraph number {i} of the long transcript." for i in range(250))
    result = await _register(SME, engagement, text=text, title="a very long interview")

    # The chunker packs short paragraphs, so assert on what actually landed
    # rather than on a chunk count the packer decides.
    (row,) = sql(
        "SELECT count(*) AS n FROM kg.chunk WHERE source_id = %(s)s",
        {"s": result["source_id"]},
    )
    assert row["n"] == result["chunks"]
    assert result["enqueued"] == result["chunks"]


async def test_chunks_are_immutable_under_the_gate_role(engagement: str, sql: Any) -> None:
    """db/017 grants INSERT and nothing else; proven from inside the suite.

    The CI denial matrix asserts the same thing against a bare role. This
    asserts it against a row this feature actually wrote, so a future
    migration that "fixed" the grant while keeping the matrix green would
    still fail here.
    """
    result = await _register(SME, engagement)
    from fde_gate.config import get_gate_settings  # noqa: PLC0415 -- test-local
    from fde_mcp import db  # noqa: PLC0415 -- test-local

    role = get_gate_settings().gate.gate_role
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        async with db.tool_transaction(role=role) as conn, conn.cursor() as cur:
            await cur.execute(
                "UPDATE kg.chunk SET content = 'rewritten' WHERE source_id = %(s)s",
                {"s": result["source_id"]},
            )


# ---------------------------------------------------------------------------
# The guardrail's claim
# ---------------------------------------------------------------------------


def _embed(sql: Any, source_id: int) -> str:
    """Give every chunk of a source the SAME embedding, and return that vector.

    Identical on purpose. It removes similarity from the experiment entirely,
    so the only difference left between the two chunks is `anchor_keys` --
    which is exactly the variable the guardrail is about.
    """
    vector = "[" + ",".join(["1"] + ["0"] * 1023) + "]"
    sql(
        "UPDATE kg.chunk SET embedding = %(v)s::vector(1024), model_id = 'pytest' "
        "WHERE source_id = %(s)s",
        {"v": vector, "s": source_id},
    )
    return vector


async def test_anchored_text_is_reachable_and_dark_text_is_not(
    engagement: str, add_node: Any, sql: Any
) -> None:
    """The claim the coverage percentage makes, proven against real retrieval.

    Both chunks are stored, both are embedded, and both are returned by
    `kg.ann_chunks` -- so neither is missing, malformed, or further away. Only
    the anchored one reaches `kg.hybrid_search`, because its chunk arm keeps
    only `cardinality(anchor_keys) > 0` (db/008_retrieval.sql:317).

    If this test ever shows the dark passage influencing a search, the
    console's "0% anchored" banner is lying and should be deleted.
    """
    add_node(ANCHOR_KEY, ANCHOR_LABEL)
    add_node(UNRELATED_KEY, UNRELATED_LABEL)
    result = await _register(SME, engagement)
    vector = _embed(sql, result["source_id"])

    # Both chunks exist, both are embedded, both are found by the raw ANN.
    ann = sql(
        "SELECT chunk_id, anchor_keys FROM kg.ann_chunks(%(eng)s::uuid, %(v)s::vector(1024), 30)",
        {"eng": engagement, "v": vector},
    )
    assert len(ann) == 2
    assert sorted(bool(r["anchor_keys"]) for r in ann) == [False, True]

    # Only the anchored one can raise a node's rank.
    hits = sql(
        """
        SELECT node_key, provenance FROM kg.hybrid_search(
          %(eng)s::uuid, %(v)s::vector(1024), 20, 30, 2, NULL, NULL, 60)
        """,
        {"eng": engagement, "v": vector},
    )
    by_key = {h["node_key"]: h["provenance"] for h in hits}

    assert ANCHOR_KEY in by_key, "the anchored passage must surface its node"
    assert "chunk_ann" in by_key[ANCHOR_KEY], "and must surface it VIA the chunk arm"
    assert "chunk_ann" not in by_key.get(UNRELATED_KEY, {}), (
        "the dark passage must not reach any node through the chunk arm"
    )


# ---------------------------------------------------------------------------
# Re-ingest
# ---------------------------------------------------------------------------


async def test_reingest_is_refused_while_nothing_new_matches(engagement: str, sql: Any) -> None:
    """A second dark copy is not progress; the refusal names the next step."""
    result = await _register(SME, engagement)
    assert result["coverage"].anchored == 0

    with pytest.raises(GateError) as excinfo:
        await sources.reingest_dark_chunks(SME, result["source_id"])
    assert excinfo.value.status == HTTPStatus.CONFLICT
    assert "ingest_interview" in excinfo.value.message

    (row,) = sql(
        "SELECT count(*) AS n FROM kg.source WHERE engagement_id = %(eng)s",
        {"eng": engagement},
    )
    assert row["n"] == 1, "a refused re-ingest must not leave a source behind"


async def test_reingest_carries_dark_chunks_into_a_new_version(
    engagement: str, add_node: Any, sql: Any
) -> None:
    """The two-pass flow, end to end: ingest dark, merge a node, re-ingest."""
    first = await _register(SME, engagement)
    assert first["coverage"].percent == 0

    add_node(ANCHOR_KEY, ANCHOR_LABEL)  # what the agent run would have proposed

    second = await sources.reingest_dark_chunks(SME, first["source_id"])
    assert second["created"] is True
    assert second["predecessor_id"] == first["source_id"]
    assert second["coverage"].anchored == 1

    (row,) = sql(
        "SELECT title, captured_at::date::text AS day, metadata, captured_by "
        "FROM kg.source WHERE source_id = %(s)s",
        {"s": second["source_id"]},
    )
    assert row["title"].endswith("(re-ingest v2)")
    assert row["metadata"]["intake"]["version"] == 2
    assert row["metadata"]["intake"]["reingest_of"] == first["source_id"]
    # The evidence was captured when it was captured, not when it was anchored.
    assert row["day"] == "2026-08-01"


async def test_reingest_never_double_counts_a_passage(
    engagement: str, add_node: Any, sql: Any
) -> None:
    """The invariant the whole versioning design rests on.

    A passage may be anchored in at most one source version. Only DARK
    chunks are carried forward, and a dark chunk is precisely one retrieval
    ignores -- so the copy left behind contributes nothing, and no text can
    be counted twice.
    """
    add_node(ANCHOR_KEY, ANCHOR_LABEL)
    first = await _register(SME, engagement)
    assert first["coverage"].anchored == 1, "one anchored, one dark to start"

    add_node(UNRELATED_KEY, "coffee")  # now the second paragraph matches too
    second = await sources.reingest_dark_chunks(SME, first["source_id"])

    # The already-anchored paragraph was NOT carried forward.
    carried = sql(
        "SELECT content FROM kg.chunk WHERE source_id = %(s)s ORDER BY ordinal",
        {"s": second["source_id"]},
    )
    assert len(carried) == 1
    assert DARK_PARAGRAPH in carried[0]["content"]
    assert ANCHORED_PARAGRAPH not in carried[0]["content"]

    # Across the whole engagement, every passage is anchored at most once.
    anchored = sql(
        """
        SELECT content, count(*) AS copies
          FROM kg.chunk
         WHERE engagement_id = %(eng)s AND cardinality(anchor_keys) > 0
         GROUP BY content
        """,
        {"eng": engagement},
    )
    assert anchored, "sanity: something is anchored"
    assert all(row["copies"] == 1 for row in anchored)


async def test_reingest_twice_with_nothing_merged_between_is_a_no_op(
    engagement: str, add_node: Any, sql: Any
) -> None:
    """Clicking the button again must not fan out a chain of identical rows."""
    first = await _register(SME, engagement)
    add_node(ANCHOR_KEY, ANCHOR_LABEL)
    second = await sources.reingest_dark_chunks(SME, first["source_id"])

    # The head is now `second`, whose remaining dark chunk still matches
    # nothing -- so the refusal is the same one the first pass would give.
    with pytest.raises(GateError) as excinfo:
        await sources.reingest_dark_chunks(SME, second["source_id"])
    assert excinfo.value.status == HTTPStatus.CONFLICT

    (row,) = sql(
        "SELECT count(*) AS n FROM kg.source WHERE engagement_id = %(eng)s",
        {"eng": engagement},
    )
    assert row["n"] == 2


async def test_reingest_of_a_superseded_version_points_at_the_head(
    engagement: str, add_node: Any
) -> None:
    first = await _register(SME, engagement)
    add_node(ANCHOR_KEY, ANCHOR_LABEL)
    second = await sources.reingest_dark_chunks(SME, first["source_id"])

    with pytest.raises(GateError) as excinfo:
        await sources.reingest_dark_chunks(SME, first["source_id"])
    assert excinfo.value.status == HTTPStatus.CONFLICT
    assert str(second["source_id"]) in excinfo.value.message


async def test_reingest_is_refused_when_everything_is_already_anchored(
    engagement: str, add_node: Any
) -> None:
    add_node(ANCHOR_KEY, ANCHOR_LABEL)
    result = await _register(SME, engagement, text=ANCHORED_PARAGRAPH)
    assert result["coverage"].dark == 0

    with pytest.raises(GateError) as excinfo:
        await sources.reingest_dark_chunks(SME, result["source_id"])
    assert "nothing dark to re-ingest" in excinfo.value.message


# ---------------------------------------------------------------------------
# The console pages
# ---------------------------------------------------------------------------


def _post(principal: str, **form: str) -> Request:
    return Request(method="POST", path="/ui/sources", form=dict(form), principal=principal)


async def test_the_preview_writes_nothing_and_shows_the_split(
    engagement: str, add_node: Any, sql: Any
) -> None:
    add_node(ANCHOR_KEY, ANCHOR_LABEL)
    response = await ui.source_preview_post(
        _post(
            SME,
            engagement_id=engagement,
            title="RevOps interview",
            source_kind="interview",
            captured_at="2026-08-01",
            text=TRANSCRIPT,
        )
    )
    assert response.status == 200
    assert isinstance(response.body, str)
    assert "50% anchored" in response.body
    assert ANCHOR_KEY in response.body
    assert "dark" in response.body

    (row,) = sql(
        "SELECT count(*) AS n FROM kg.source WHERE engagement_id = %(eng)s",
        {"eng": engagement},
    )
    assert row["n"] == 0, "preview must not write"


async def test_a_zero_coverage_preview_names_the_consequence_and_the_next_step(
    engagement: str,
) -> None:
    """The banner is the feature. Its two halves are asserted verbatim."""
    response = await ui.source_preview_post(
        _post(
            SME,
            engagement_id=engagement,
            title="RevOps interview",
            source_kind="interview",
            captured_at="2026-08-01",
            text=TRANSCRIPT,
        )
    )
    assert isinstance(response.body, str)
    assert "0% anchored" in response.body
    assert "No passage in this document matches anything in the graph yet." in response.body
    assert "Retrieval will never see this text." in response.body
    assert "ingest_interview" in response.body
    assert "Re-ingest dark chunks" in response.body


async def test_saving_from_the_preview_lands_on_the_new_source(
    engagement: str, add_node: Any
) -> None:
    add_node(ANCHOR_KEY, ANCHOR_LABEL)
    response = await ui.source_create_post(
        _post(
            SME,
            engagement_id=engagement,
            title="RevOps interview",
            source_kind="interview",
            captured_at="2026-08-01",
            text=TRANSCRIPT,
        )
    )
    assert response.status == HTTPStatus.SEE_OTHER
    location = response.headers["Location"]
    assert location.startswith("/ui/sources/")
    assert "1 of them" in unquote(location) or "searchable now" in unquote(location)


async def test_an_uploaded_file_is_ingested_and_its_name_recorded(
    engagement: str, sql: Any
) -> None:
    request = Request(
        method="POST",
        path="/ui/sources",
        form={
            "engagement_id": engagement,
            "title": "SOP from a file",
            "source_kind": "sop_document",
            "captured_at": "2026-08-01",
        },
        files={"file": Upload(filename="sop.md", content=TRANSCRIPT)},
        principal=SME,
    )
    response = await ui.source_create_post(request)
    assert response.status == HTTPStatus.SEE_OTHER

    (row,) = sql(
        "SELECT metadata FROM kg.source WHERE engagement_id = %(eng)s",
        {"eng": engagement},
    )
    assert row["metadata"]["intake"]["captured_from"] == "upload"
    assert row["metadata"]["intake"]["filename"] == "sop.md"


async def test_an_upload_with_the_wrong_extension_is_refused_with_the_form_intact(
    engagement: str,
) -> None:
    """A rejected submission must not discard what the operator typed."""
    request = Request(
        method="POST",
        path="/ui/sources",
        form={
            "engagement_id": engagement,
            "title": "A title worth keeping",
            "source_kind": "interview",
            "captured_at": "2026-08-01",
        },
        files={"file": Upload(filename="notes.pdf", content="whatever")},
        principal=SME,
    )
    response = await ui.source_preview_post(request)

    assert response.status == HTTPStatus.BAD_REQUEST
    assert isinstance(response.body, str)
    assert "not a text file" in response.body
    assert "A title worth keeping" in response.body, "the form comes back filled in"


async def test_pasting_and_uploading_at_once_is_refused_rather_than_guessed(
    engagement: str,
) -> None:
    request = Request(
        method="POST",
        path="/ui/sources",
        form={
            "engagement_id": engagement,
            "title": "Ambiguous",
            "source_kind": "interview",
            "captured_at": "2026-08-01",
            "text": "pasted text",
        },
        files={"file": Upload(filename="also.txt", content="uploaded text")},
        principal=SME,
    )
    response = await ui.source_preview_post(request)
    assert response.status == HTTPStatus.BAD_REQUEST
    assert isinstance(response.body, str)
    assert "Use one or the other" in response.body


async def test_the_source_list_shows_coverage_and_the_supersession_chain(
    engagement: str, add_node: Any
) -> None:
    first = await _register(SME, engagement)
    add_node(ANCHOR_KEY, ANCHOR_LABEL)
    second = await sources.reingest_dark_chunks(SME, first["source_id"])

    response = await ui.sources_page(
        Request(
            method="GET",
            path="/ui/sources",
            query={"engagement_id": engagement},
            principal=SME,
        )
    )
    assert response.status == 200
    assert isinstance(response.body, str)
    assert "0%" in response.body
    assert "superseded by" in response.body
    assert f"/ui/sources/{second['source_id']}" in response.body


async def test_the_detail_page_offers_reingest_only_on_the_head(
    engagement: str, add_node: Any
) -> None:
    first = await _register(SME, engagement)
    add_node(ANCHOR_KEY, ANCHOR_LABEL)
    second = await sources.reingest_dark_chunks(SME, first["source_id"])

    def _get(source_id: int) -> Request:
        return Request(
            method="GET",
            path=f"/ui/sources/{source_id}",
            path_params={"source_id": str(source_id)},
            principal=SME,
        )

    superseded = await ui.source_page(_get(first["source_id"]))
    assert isinstance(superseded.body, str)
    assert "has been re-ingested as" in superseded.body
    assert f'action="/ui/sources/{first["source_id"]}/reingest"' not in superseded.body

    head = await ui.source_page(_get(second["source_id"]))
    assert isinstance(head.body, str)
    # Whitespace-collapsed: the button label wraps across lines in the
    # template, and asserting on the rendered indentation would break on a
    # reformat that changed nothing an operator sees.
    assert "Re-ingest 1 dark passage" in " ".join(head.body.split())


async def test_a_superseded_version_does_not_repeat_the_fix_it_instruction(
    engagement: str, add_node: Any
) -> None:
    """Its dark copies are meant to stay dark; saying otherwise is busywork.

    The superseded version keeps 0% coverage forever by design -- that is
    what makes the re-ingest safe from double-counting. Repeating "run
    ingest_interview, then re-ingest" on this page would send the operator
    to redo work the supersession notice says is already done.
    """
    first = await _register(SME, engagement)
    add_node(ANCHOR_KEY, ANCHOR_LABEL)
    await sources.reingest_dark_chunks(SME, first["source_id"])

    response = await ui.source_page(
        Request(
            method="GET",
            path=f"/ui/sources/{first['source_id']}",
            path_params={"source_id": str(first["source_id"])},
            principal=SME,
        )
    )
    assert isinstance(response.body, str)
    assert "has been re-ingested as" in response.body
    assert sources.DARK_NEXT_STEP not in response.body
    assert "No passage in this source is searchable" not in response.body


async def test_a_missing_source_renders_not_found(engagement: str) -> None:
    response = await ui.source_page(
        Request(
            method="GET",
            path="/ui/sources/999999999",
            path_params={"source_id": "999999999"},
            principal=SME,
        )
    )
    assert isinstance(response.body, str)
    assert "999999999" in response.body


async def test_transcript_markup_is_escaped_on_every_intake_page(engagement: str, sql: Any) -> None:
    """A transcript is a document from a customer; it can contain anything."""
    hostile = '<script>alert("xss")</script>\n\nA second paragraph entirely.'
    result = await _register(SME, engagement, text=hostile, title="<b>bold title</b>")

    response = await ui.source_page(
        Request(
            method="GET",
            path=f"/ui/sources/{result['source_id']}",
            path_params={"source_id": str(result["source_id"])},
            principal=SME,
        )
    )
    assert isinstance(response.body, str)
    assert "<script>" not in response.body
    assert "&lt;script&gt;" in response.body
    assert "<b>bold title</b>" not in response.body
