"""Reviewer administration: the admin gate, and the grants underneath it.

The page writes `hitl.reviewer`, `hitl.reviewer_authority` and
`hitl.reviewer_admin` -- the roster that decides who may clear a gate. Two
different things therefore have to hold, and they are tested separately
because they fail separately:

* **Authorisation** (this file, most of it): a principal without the admin
  authority is refused on GET and on every POST, and refused with a message
  rather than a redirect that pretends it worked.
* **Privilege** (`test_grants_are_gate_service_only`, and the CI denial
  matrix in `.github/workflows/ci.yml`): no role but `fde_gate_service` may
  write those tables at all, so a bug in the layer above cannot become a
  roster edit by `fde_agent`.

Reviewer rows are global and this suite shares a database with itself across
runs, so every reviewer these tests create carries a uuid suffix, and the
one test that needs "exactly one admin exists" makes that true for its own
duration and puts back what it found.
"""

from __future__ import annotations

import uuid
from http import HTTPStatus
from typing import Any
from urllib.parse import unquote

import pytest
from gate_seed import OWNER, SME, STRANGER

from fde_gate import ui
from fde_gate.http import Request

pytestmark = pytest.mark.requires_db


def _principal() -> str:
    return f"pytest-{uuid.uuid4().hex[:10]}@example.com"


@pytest.fixture
def admin(seed: dict[str, Any], sql: Any) -> str:
    """OWNER, holding a live admin grant.

    Granted here as the owner rather than through the console, because the
    first admin on any deployment is granted exactly this way -- there is no
    other way for one to come into existence (db/016).
    """
    sql(
        """
        INSERT INTO hitl.reviewer_admin (reviewer_id, granted_by)
        SELECT reviewer_id, 'pytest' FROM hitl.reviewer WHERE principal = %(p)s
        ON CONFLICT (reviewer_id) DO UPDATE SET revoked_at = NULL
        """,
        {"p": OWNER},
    )
    return OWNER


@pytest.fixture
def a_reviewer(admin: str, sql: Any) -> dict[str, Any]:
    """A throwaway reviewer row to administer."""
    principal = _principal()
    (row,) = sql(
        """
        INSERT INTO hitl.reviewer (principal, display_name)
        VALUES (%(p)s, 'Pytest Reviewer') RETURNING reviewer_id, principal
        """,
        {"p": principal},
    )
    return dict(row)


def _get(principal: str) -> Request:
    return Request(method="GET", path="/ui/reviewers", principal=principal)


def _post(actor: str, reviewer_id: int | None = None, **form: str) -> Request:
    """`actor` is who is clicking; a `principal` in **form is who they are
    administering. Naming the first one `principal` here made the add-reviewer
    call a TypeError, which is the friendly version of the confusion the
    service module's header warns about.
    """
    params = {} if reviewer_id is None else {"reviewer_id": str(reviewer_id)}
    return Request(
        method="POST",
        path="/ui/reviewers",
        path_params=params,
        form=form,
        principal=actor,
    )


# ---------------------------------------------------------------------------
# Authorisation
# ---------------------------------------------------------------------------


async def test_non_admin_is_refused_on_get(admin: str) -> None:
    """SME holds two gate authorities and no admin authority.

    Using a real, authorised reviewer rather than a stranger is the point:
    the page must be gated on the ADMIN authority specifically, not on being
    a known reviewer.
    """
    page = await ui.reviewers_page(_get(SME))

    assert page.status == HTTPStatus.FORBIDDEN
    assert page.content_type.startswith("text/html"), "an operator gets a page, not JSON"
    assert isinstance(page.body, str)
    assert "Not authorised" in page.body
    assert SME in page.body
    assert "does not hold the admin authority" in " ".join(page.body.split())
    # The refusal names the fix, per the console's error posture.
    assert "docs/10" in page.body


@pytest.mark.parametrize(
    ("handler_name", "form"),
    [
        ("reviewer_add_post", {"principal": "mallory@example.com", "display_name": "M"}),
        ("reviewer_active_post", {"active": "0"}),
        (
            "reviewer_authority_post",
            {
                "action": "grant",
                "engagement_id": "11111111-1111-1111-1111-111111111111",
                "gate_kind": "control",
            },
        ),
        ("reviewer_admin_post", {"admin": "1"}),
    ],
)
async def test_non_admin_is_refused_on_every_post(
    handler_name: str, form: dict[str, str], a_reviewer: dict[str, Any], sql: Any
) -> None:
    """Every mutation, not just the page, and nothing is written."""
    before = sql("SELECT count(*) AS n FROM hitl.reviewer")[0]["n"]

    handler = getattr(ui, handler_name)
    response = await handler(_post(STRANGER, a_reviewer["reviewer_id"], **form))

    assert response.status == HTTPStatus.FORBIDDEN, handler_name
    assert isinstance(response.body, str)
    assert "Not authorised" in response.body

    assert sql("SELECT count(*) AS n FROM hitl.reviewer")[0]["n"] == before
    (target,) = sql(
        "SELECT is_active, hitl.is_reviewer_admin(principal) AS is_admin "
        "FROM hitl.reviewer WHERE reviewer_id = %(r)s",
        {"r": a_reviewer["reviewer_id"]},
    )
    assert target["is_active"] is True, "a refused deactivation must not deactivate"
    assert target["is_admin"] is False, "a refused admin grant must not grant"
    assert (
        sql(
            "SELECT count(*) AS n FROM hitl.reviewer_authority WHERE reviewer_id = %(r)s",
            {"r": a_reviewer["reviewer_id"]},
        )[0]["n"]
        == 0
    )


async def test_the_nav_link_is_admin_only(admin: str) -> None:
    """The link exists for an admin and not for anyone else.

    Checked on the queue page rather than the reviewers page, because the
    link lives in the chrome every page extends -- that is what makes the
    page findable at all.
    """
    for_admin = await ui.queue_page(Request(method="GET", path="/ui", principal=admin))
    for_reviewer = await ui.queue_page(Request(method="GET", path="/ui", principal=SME))

    assert isinstance(for_admin.body, str)
    assert isinstance(for_reviewer.body, str)
    assert '<a href="/ui/reviewers">Reviewers</a>' in for_admin.body
    assert "/ui/reviewers" not in for_reviewer.body


async def test_revoking_admin_takes_the_page_away(admin: str, sql: Any) -> None:
    """The check is live, not a property of the session."""
    assert (await ui.reviewers_page(_get(admin))).status == HTTPStatus.OK

    sql(
        "UPDATE hitl.reviewer_admin SET revoked_at = now() "
        "WHERE reviewer_id = (SELECT reviewer_id FROM hitl.reviewer WHERE principal = %(p)s)",
        {"p": admin},
    )

    assert (await ui.reviewers_page(_get(admin))).status == HTTPStatus.FORBIDDEN


async def test_deactivating_an_admin_also_removes_their_admin_rights(
    admin: str, a_reviewer: dict[str, Any], sql: Any
) -> None:
    """`is_active` gates the admin authority too, so "deactivate" is complete."""
    rid = a_reviewer["reviewer_id"]
    await ui.reviewer_admin_post(_post(admin, rid, admin="1"))
    assert sql("SELECT hitl.is_reviewer_admin(%(p)s) AS a", {"p": a_reviewer["principal"]})[0]["a"]

    await ui.reviewer_active_post(_post(admin, rid, active="0"))

    assert not sql("SELECT hitl.is_reviewer_admin(%(p)s) AS a", {"p": a_reviewer["principal"]})[0][
        "a"
    ]
    assert (await ui.reviewers_page(_get(a_reviewer["principal"]))).status == HTTPStatus.FORBIDDEN


# ---------------------------------------------------------------------------
# The administration itself
# ---------------------------------------------------------------------------


async def test_admin_sees_the_roster(admin: str, a_reviewer: dict[str, Any]) -> None:
    page = await ui.reviewers_page(_get(admin))

    assert page.status == HTTPStatus.OK
    assert isinstance(page.body, str)
    assert a_reviewer["principal"] in page.body
    assert "cannot clear any gate" in page.body, "a reviewer with no authority says so"
    # The gate kinds offered come from the enum, so all four are there.
    for kind in ("ontology", "factual", "control", "automation"):
        assert f'<option value="{kind}">' in page.body


async def test_add_reviewer_roundtrip(admin: str, sql: Any) -> None:
    principal = _principal()
    response = await ui.reviewer_add_post(
        _post(admin, None, principal=principal, display_name="Added By Test", email="a@b.example")
    )

    assert response.status == HTTPStatus.SEE_OTHER
    assert response.headers["Location"].startswith("/ui/reviewers?notice=")
    (row,) = sql(
        "SELECT display_name, email, is_active FROM hitl.reviewer WHERE principal = %(p)s",
        {"p": principal},
    )
    assert row["display_name"] == "Added By Test"
    assert row["is_active"] is True
    # A new reviewer holds nothing until something is granted.
    assert not sql("SELECT hitl.is_reviewer_admin(%(p)s) AS a", {"p": principal})[0]["a"]


async def test_a_duplicate_principal_is_refused_with_the_databases_words(
    admin: str, a_reviewer: dict[str, Any]
) -> None:
    response = await ui.reviewer_add_post(
        _post(admin, None, principal=a_reviewer["principal"], display_name="Impostor")
    )

    assert response.status == HTTPStatus.SEE_OTHER
    location = unquote(response.headers["Location"])
    assert "error=" in location
    assert "reviewer_principal_key" in location, "the unique violation reaches the operator"


async def test_authority_grant_revoke_and_regrant(
    admin: str, a_reviewer: dict[str, Any], seed: dict[str, Any], sql: Any
) -> None:
    """Revoking stamps the row; re-granting reuses it.

    The primary key is (reviewer_id, engagement_id, gate_kind), so a second
    grant of the same authority is an UPDATE -- which is exactly the write
    db/016 keeps column-scoped, and would fail with permission denied if the
    grant list were wrong.
    """
    rid = a_reviewer["reviewer_id"]
    eng = seed["engagement_id"]
    form = {"engagement_id": eng, "gate_kind": "control"}

    granted = await ui.reviewer_authority_post(_post(admin, rid, action="grant", **form))
    assert granted.status == HTTPStatus.SEE_OTHER
    assert "granted control" in unquote(granted.headers["Location"])
    (row,) = sql(
        "SELECT granted_by, revoked_at FROM hitl.reviewer_authority "
        "WHERE reviewer_id = %(r)s AND engagement_id = %(e)s::uuid AND gate_kind = 'control'",
        {"r": rid, "e": eng},
    )
    assert row["revoked_at"] is None
    assert row["granted_by"] == admin, "the acting admin is recorded, not the form"

    revoked = await ui.reviewer_authority_post(_post(admin, rid, action="revoke", **form))
    # "revoked", not "revokeed" -- the notice is the only confirmation the
    # operator gets that the button did what its label said.
    assert "revoked control" in unquote(revoked.headers["Location"])
    (row,) = sql(
        "SELECT revoked_at FROM hitl.reviewer_authority "
        "WHERE reviewer_id = %(r)s AND engagement_id = %(e)s::uuid AND gate_kind = 'control'",
        {"r": rid, "e": eng},
    )
    assert row["revoked_at"] is not None, "revocation is a stamp, and the row survives it"

    await ui.reviewer_authority_post(_post(admin, rid, action="grant", **form))
    rows = sql(
        "SELECT revoked_at FROM hitl.reviewer_authority "
        "WHERE reviewer_id = %(r)s AND engagement_id = %(e)s::uuid AND gate_kind = 'control'",
        {"r": rid, "e": eng},
    )
    assert len(rows) == 1, "re-granting updates the row rather than adding a second"
    assert rows[0]["revoked_at"] is None


async def test_revoking_an_authority_nobody_holds_says_so(
    admin: str, a_reviewer: dict[str, Any], seed: dict[str, Any]
) -> None:
    response = await ui.reviewer_authority_post(
        _post(
            admin,
            a_reviewer["reviewer_id"],
            action="revoke",
            engagement_id=seed["engagement_id"],
            gate_kind="factual",
        )
    )
    assert response.status == HTTPStatus.SEE_OTHER
    assert "holds no live factual authority" in unquote(response.headers["Location"])


async def test_deactivate_is_an_update_never_a_delete(
    admin: str, a_reviewer: dict[str, Any], sql: Any
) -> None:
    """The row survives, because gate decisions reference it."""
    rid = a_reviewer["reviewer_id"]

    await ui.reviewer_active_post(_post(admin, rid, active="0"))
    rows = sql("SELECT is_active FROM hitl.reviewer WHERE reviewer_id = %(r)s", {"r": rid})
    assert len(rows) == 1, "deactivation must never remove the row"
    assert rows[0]["is_active"] is False

    await ui.reviewer_active_post(_post(admin, rid, active="1"))
    assert sql("SELECT is_active FROM hitl.reviewer WHERE reviewer_id = %(r)s", {"r": rid})[0][
        "is_active"
    ]


async def test_granting_admin_to_a_deactivated_reviewer_is_refused(
    admin: str, a_reviewer: dict[str, Any], sql: Any
) -> None:
    """It would be a row that exists and does nothing."""
    rid = a_reviewer["reviewer_id"]
    await ui.reviewer_active_post(_post(admin, rid, active="0"))

    response = await ui.reviewer_admin_post(_post(admin, rid, admin="1"))

    assert response.status == HTTPStatus.SEE_OTHER
    assert "reactivate them before granting admin" in unquote(response.headers["Location"])
    assert (
        sql("SELECT count(*) AS n FROM hitl.reviewer_admin WHERE reviewer_id = %(r)s", {"r": rid})[
            0
        ]["n"]
        == 0
    )


async def test_admin_grant_revoke_and_regrant_roundtrip(
    admin: str, a_reviewer: dict[str, Any], sql: Any
) -> None:
    """A second admin is appointed, removed, and reappointed, end to end.

    The revoke leg is the one the "last admin" test cannot reach: it refuses
    before writing. This is the only coverage of the successful
    `UPDATE hitl.reviewer_admin SET revoked_at` and of `set_admin`'s
    ON CONFLICT DO UPDATE branch, both executed as `fde_gate_service` under
    the column-scoped grant from db/016 -- either would fail with permission
    denied if that grant list were wrong.
    """
    rid = a_reviewer["reviewer_id"]
    target = a_reviewer["principal"]

    def rows() -> list[dict[str, Any]]:
        return sql(
            "SELECT granted_by, granted_at, revoked_at FROM hitl.reviewer_admin "
            "WHERE reviewer_id = %(r)s",
            {"r": rid},
        )

    def is_admin_now() -> bool:
        return bool(sql("SELECT hitl.is_reviewer_admin(%(p)s) AS a", {"p": target})[0]["a"])

    granted = await ui.reviewer_admin_post(_post(admin, rid, admin="1"))
    assert granted.status == HTTPStatus.SEE_OTHER
    assert "admin granted to" in unquote(granted.headers["Location"])
    (row,) = rows()
    assert row["revoked_at"] is None
    assert row["granted_by"] == admin
    assert is_admin_now()
    # Appointed, and the page is really theirs now.
    assert (await ui.reviewers_page(_get(target))).status == HTTPStatus.OK
    first_granted_at = row["granted_at"]

    # Allowed because `admin` is still a live administrator, so this is not
    # the last one.
    revoked = await ui.reviewer_admin_post(_post(admin, rid, admin="0"))
    assert revoked.status == HTTPStatus.SEE_OTHER
    assert "admin revoked from" in unquote(revoked.headers["Location"])
    (row,) = rows()
    assert row["revoked_at"] is not None, "revocation is a stamp on the existing row"
    assert not is_admin_now()
    assert (await ui.reviewers_page(_get(target))).status == HTTPStatus.FORBIDDEN

    regranted = await ui.reviewer_admin_post(_post(admin, rid, admin="1"))
    assert regranted.status == HTTPStatus.SEE_OTHER
    all_rows = rows()
    assert len(all_rows) == 1, "re-granting updates the row rather than adding a second"
    assert all_rows[0]["revoked_at"] is None
    assert all_rows[0]["granted_at"] >= first_granted_at, "the live grant is re-stamped"
    assert is_admin_now()


async def test_the_last_admin_cannot_be_removed(
    admin: str, a_reviewer: dict[str, Any], sql: Any
) -> None:
    """Otherwise the console can lock every human out of the console.

    The rule is global, so this test makes its subject the only live admin
    for its own duration and restores the rest afterwards.
    """
    rid = a_reviewer["reviewer_id"]
    await ui.reviewer_admin_post(_post(admin, rid, admin="1"))

    parked = [
        row["reviewer_id"]
        for row in sql(
            "SELECT reviewer_id FROM hitl.reviewer_admin "
            "WHERE revoked_at IS NULL AND reviewer_id <> %(r)s",
            {"r": rid},
        )
    ]
    sql(
        "UPDATE hitl.reviewer_admin SET revoked_at = now() WHERE reviewer_id = ANY(%(ids)s)",
        {"ids": parked},
    )
    try:
        # The sole remaining admin, acting as themselves, on themselves.
        actor = a_reviewer["principal"]
        revoked = await ui.reviewer_admin_post(_post(actor, rid, admin="0"))
        deactivated = await ui.reviewer_active_post(_post(actor, rid, active="0"))

        for response in (revoked, deactivated):
            assert response.status == HTTPStatus.SEE_OTHER
            assert "only reviewer holding a live admin authority" in unquote(
                response.headers["Location"]
            )
        assert sql("SELECT hitl.is_reviewer_admin(%(p)s) AS a", {"p": actor})[0]["a"]
    finally:
        sql(
            "UPDATE hitl.reviewer_admin SET revoked_at = NULL WHERE reviewer_id = ANY(%(ids)s)",
            {"ids": parked},
        )


async def test_a_hostile_display_name_is_escaped(admin: str, sql: Any) -> None:
    """The roster is operator-entered text rendered back to operators."""
    hostile = '<script>alert("xss")</script>'
    principal = _principal()
    sql(
        "INSERT INTO hitl.reviewer (principal, display_name) VALUES (%(p)s, %(d)s)",
        {"p": principal, "d": hostile},
    )

    page = await ui.reviewers_page(_get(admin))

    assert isinstance(page.body, str)
    assert "alert" in page.body, "the name reached the page..."
    assert hostile not in page.body, "...but not as markup"
    assert "<script>" not in page.body


# ---------------------------------------------------------------------------
# Privilege
# ---------------------------------------------------------------------------


async def test_grants_are_gate_service_only(sql: Any) -> None:
    """No role but `fde_gate_service` may write the roster, and not even it
    may DELETE. The CI denial matrix red-teams this by running the
    statements; this asserts the grant SHAPE, so a later migration that
    widens it fails here with a name rather than in a shell script.
    """
    grants = sql(
        """
        SELECT grantee, table_name, privilege_type
          FROM information_schema.role_table_grants
         WHERE table_schema = 'hitl'
           AND table_name IN ('reviewer','reviewer_authority','reviewer_admin')
           AND privilege_type IN ('INSERT','UPDATE','DELETE','TRUNCATE')
           AND grantee <> CURRENT_USER
        """
    )
    assert {row["grantee"] for row in grants} == {"fde_gate_service"}, (
        "only the gate service may write the reviewer tables"
    )
    assert not [row for row in grants if row["privilege_type"] == "DELETE"], (
        "deactivation only: nobody may DELETE a reviewer, or the decision trail goes with them"
    )
    assert not [row for row in grants if row["privilege_type"] == "TRUNCATE"]

    # UPDATE is column-scoped, so it does not appear as a table grant at all
    # -- it lives in column_privileges, and only for the columns db/016 lists.
    assert not [row for row in grants if row["privilege_type"] == "UPDATE"], (
        "a table-wide UPDATE would let a console bug rewrite hitl.reviewer.principal"
    )
    columns = sql(
        """
        SELECT table_name, column_name
          FROM information_schema.column_privileges
         WHERE table_schema = 'hitl'
           AND table_name IN ('reviewer','reviewer_authority','reviewer_admin')
           AND privilege_type = 'UPDATE'
           AND grantee = 'fde_gate_service'
        """
    )
    assert {(row["table_name"], row["column_name"]) for row in columns} == {
        ("reviewer", "is_active"),
        ("reviewer_authority", "granted_by"),
        ("reviewer_authority", "granted_at"),
        ("reviewer_authority", "revoked_at"),
        ("reviewer_admin", "granted_by"),
        ("reviewer_admin", "granted_at"),
        ("reviewer_admin", "revoked_at"),
    }


async def test_the_admin_predicate_is_not_public(sql: Any) -> None:
    """Functions arrive with EXECUTE granted to PUBLIC; db/016 revokes it."""
    (row,) = sql(
        """
        SELECT has_function_privilege('fde_agent',
                 'hitl.is_reviewer_admin(text)', 'EXECUTE') AS agent,
               has_function_privilege('fde_gate_service',
                 'hitl.is_reviewer_admin(text)', 'EXECUTE') AS gate
        """
    )
    assert row["agent"] is False, "REVOKE ALL ... FROM PUBLIC must hold"
    assert row["gate"] is True
