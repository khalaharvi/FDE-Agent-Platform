"""service/reviewers.py -- the roster: who may review, and what they may clear.

Runs as `fde_gate_service`, the only role in the platform that may write
`hitl.reviewer`, `hitl.reviewer_authority` and `hitl.reviewer_admin`
(db/016). Every other role, `fde_agent` included, is denied at the table --
asserted in CI rather than trusted, alongside the eight core denials.

Two things this module does that the other service modules do not:

* **It authorises before it acts.** `proposals.py` can hand the whole
  question to the database, because `hitl.record_decision` re-checks the
  reviewer's authority itself and refuses with a message worth reading.
  There is no equivalent function here -- these are ordinary INSERTs and
  UPDATEs -- so `_assert_admin` runs first, INSIDE the same transaction as
  the write. A check on a separate connection would leave a window in which
  admin is revoked between the check and the write.
* **It refuses to remove the last administrator.** Deactivating the only
  admin, or revoking their grant, would leave a console nobody can
  administer and a recovery path that is exactly the hand-written SQL this
  page exists to abolish.

`actor` versus `principal`
--------------------------
Everywhere else in this package `principal` is the caller. Here a reviewer
row HAS a `principal` column, so the caller is `actor` and `principal` is
always the subject being administered. The two are different people in
almost every call, and one name for both is how an authorisation check ends
up run against the wrong identity.
"""

from __future__ import annotations

from http import HTTPStatus
from typing import Any

from fde_gate.config import get_gate_settings
from fde_gate.http import GateError
from fde_gate.rows import fetchall, fetchone
from fde_mcp import db
from fde_mcp.logging import get_logger

log = get_logger(__name__)

__all__ = [
    "add_reviewer",
    "grant_authority",
    "is_admin",
    "list_roster",
    "revoke_authority",
    "set_active",
    "set_admin",
]

# The console's own words for a refusal, naming what would fix it. An
# operator who lands here has been sent a link by a colleague; "403" alone
# leaves them unable to tell a missing grant from a broken page.
_NOT_ADMIN = (
    "{actor} does not hold the admin authority, which is what /ui/reviewers "
    "requires. Ask someone who does to grant it to you on this page; on a new "
    "deployment the first admin is granted once, by hand, per docs/10."
)

_LAST_ADMIN = (
    "{principal} is the only reviewer holding a live admin authority. Grant "
    "admin to someone else first -- removing the last one leaves a console "
    "nobody can administer."
)


def _gate_role() -> str:
    return get_gate_settings().gate.gate_role


async def _assert_admin(cur: Any, actor: str) -> None:
    """Refuse anyone but a live admin. Called first, in the caller's transaction."""
    await cur.execute("SELECT hitl.is_reviewer_admin(%(a)s) AS ok", {"a": actor})
    row = await fetchone(cur)
    if row is None or not row["ok"]:
        raise GateError(HTTPStatus.FORBIDDEN, _NOT_ADMIN.format(actor=actor or "anonymous"))


async def _assert_not_last_admin(cur: Any, reviewer_id: int) -> None:
    """Refuse to strip admin from the last reviewer who has it."""
    await cur.execute(
        """
        SELECT r.principal,
               EXISTS (SELECT 1
                         FROM hitl.reviewer_admin a2
                         JOIN hitl.reviewer r2 ON r2.reviewer_id = a2.reviewer_id
                        WHERE a2.revoked_at IS NULL
                          AND r2.is_active
                          AND r2.reviewer_id <> %(rid)s) AS another_admin_exists,
               hitl.is_reviewer_admin(r.principal) AS is_admin
          FROM hitl.reviewer r
         WHERE r.reviewer_id = %(rid)s
        """,
        {"rid": reviewer_id},
    )
    row = await fetchone(cur)
    if row is None:
        raise GateError(HTTPStatus.NOT_FOUND, f"no reviewer {reviewer_id}")
    if row["is_admin"] and not row["another_admin_exists"]:
        raise GateError(HTTPStatus.CONFLICT, _LAST_ADMIN.format(principal=row["principal"]))


async def is_admin(actor: str) -> bool:
    """Does this principal hold a live admin authority?

    The console asks this on every page render, to decide whether the
    Reviewers link exists. It is deliberately NOT the enforcement point --
    every function below re-asks inside its own transaction, so a hidden
    link and a refused POST are two independent answers rather than one
    answer trusted twice.
    """
    if not actor:
        return False
    async with db.tool_transaction(role=_gate_role()) as conn, conn.cursor() as cur:
        await cur.execute("SELECT hitl.is_reviewer_admin(%(a)s) AS ok", {"a": actor})
        row = await fetchone(cur)
    return bool(row is not None and row["ok"])


async def list_roster(actor: str) -> dict[str, Any]:
    """Every reviewer, with their live authorities and admin standing.

    Inactive reviewers are listed, not hidden: they still appear as the
    author of past decisions, and an operator looking for "why can't Dana
    clear this" needs to find Dana. They sort last.
    """
    async with db.tool_transaction(role=_gate_role()) as conn, conn.cursor() as cur:
        await _assert_admin(cur, actor)

        await cur.execute(
            """
            SELECT r.reviewer_id, r.principal, r.display_name, r.email,
                   r.is_active, r.created_at,
                   (a.reviewer_id IS NOT NULL) AS is_admin,
                   a.granted_by AS admin_granted_by,
                   COALESCE(
                     (SELECT jsonb_agg(jsonb_build_object(
                               'engagement_id', ra.engagement_id,
                               'gate_kind',     ra.gate_kind,
                               'granted_by',    ra.granted_by,
                               'granted_at',    ra.granted_at)
                             ORDER BY ra.engagement_id, ra.gate_kind)
                        FROM hitl.reviewer_authority ra
                       WHERE ra.reviewer_id = r.reviewer_id
                         AND ra.revoked_at IS NULL),
                     '[]'::jsonb) AS authorities
              FROM hitl.reviewer r
              LEFT JOIN hitl.reviewer_admin a
                ON a.reviewer_id = r.reviewer_id AND a.revoked_at IS NULL
             ORDER BY r.is_active DESC, r.principal
            """
        )
        roster = await fetchall(cur)

        # The gate kinds come from the enum rather than a tuple in this file:
        # db/001 is the definition, and a hard-coded list here would offer a
        # kind the database would then reject on submit.
        await cur.execute("SELECT unnest(enum_range(NULL::hitl.gate_kind))::text AS gate_kind")
        gate_kinds = [row["gate_kind"] for row in await fetchall(cur)]

        # Engagements worth suggesting: any the platform has already seen.
        # A uuid is not a thing anyone types correctly from memory.
        await cur.execute(
            """
            SELECT DISTINCT engagement_id FROM (
              SELECT engagement_id FROM hitl.proposal
              UNION
              SELECT engagement_id FROM hitl.reviewer_authority
            ) e ORDER BY engagement_id
            """
        )
        engagements = [row["engagement_id"] for row in await fetchall(cur)]

    return {
        "actor": actor,
        "reviewers": roster,
        "gate_kinds": gate_kinds,
        "engagements": engagements,
    }


async def add_reviewer(
    actor: str, *, principal: str, display_name: str, email: str | None = None
) -> dict[str, Any]:
    """Register a person. They hold no authority until one is granted.

    A reviewer row on its own confers nothing -- `hitl.record_decision`
    refuses a principal with no live authority for the gate kind -- so
    adding someone is a safe, reversible act, and the authority grant is the
    decision worth thinking about.
    """
    async with db.tool_transaction(role=_gate_role()) as conn, conn.cursor() as cur:
        await _assert_admin(cur, actor)
        await cur.execute(
            """
            INSERT INTO hitl.reviewer (principal, display_name, email)
            VALUES (%(p)s, %(d)s, %(e)s)
            RETURNING reviewer_id, principal, display_name, email, is_active
            """,
            {"p": principal, "d": display_name, "e": email or None},
        )
        reviewer = await fetchone(cur)
    log.info("reviewer_added", actor=actor, principal=principal)
    return {"reviewer": reviewer}


async def set_active(actor: str, reviewer_id: int, *, active: bool) -> dict[str, Any]:
    """Deactivate or reactivate a reviewer. Never a DELETE.

    `hitl.gate_decision.reviewer_id` references this row, so removing it
    would remove the record of who signed off on what. Deactivating is the
    whole of "remove" here, and it is complete: `record_decision` refuses an
    inactive reviewer, `gates_satisfied` stops counting their approvals, and
    `hitl.is_reviewer_admin` stops returning true for them.
    """
    async with db.tool_transaction(role=_gate_role()) as conn, conn.cursor() as cur:
        await _assert_admin(cur, actor)
        if not active:
            await _assert_not_last_admin(cur, reviewer_id)
        await cur.execute(
            """
            UPDATE hitl.reviewer SET is_active = %(a)s
             WHERE reviewer_id = %(rid)s
            RETURNING reviewer_id, principal, display_name, is_active
            """,
            {"a": active, "rid": reviewer_id},
        )
        reviewer = await fetchone(cur)
        if reviewer is None:
            raise GateError(HTTPStatus.NOT_FOUND, f"no reviewer {reviewer_id}")
    log.info("reviewer_active_set", actor=actor, reviewer_id=reviewer_id, is_active=active)
    return {"reviewer": reviewer}


async def grant_authority(
    actor: str, reviewer_id: int, *, engagement_id: str, gate_kind: str
) -> dict[str, Any]:
    """Grant one gate kind on one engagement.

    Re-granting something previously revoked updates the existing row rather
    than inserting a second one -- the primary key is
    (reviewer_id, engagement_id, gate_kind) -- so `granted_by` and
    `granted_at` always describe the grant that is currently in force.
    """
    async with db.tool_transaction(role=_gate_role()) as conn, conn.cursor() as cur:
        await _assert_admin(cur, actor)
        await cur.execute(
            """
            INSERT INTO hitl.reviewer_authority
                (reviewer_id, engagement_id, gate_kind, granted_by)
            VALUES (%(rid)s, %(eng)s::uuid, %(k)s::hitl.gate_kind, %(by)s)
            ON CONFLICT (reviewer_id, engagement_id, gate_kind) DO UPDATE
               SET revoked_at = NULL,
                   granted_by = EXCLUDED.granted_by,
                   granted_at = now()
            RETURNING reviewer_id, engagement_id, gate_kind::text AS gate_kind,
                      granted_by, granted_at, revoked_at
            """,
            {"rid": reviewer_id, "eng": engagement_id, "k": gate_kind, "by": actor},
        )
        authority = await fetchone(cur)
    log.info(
        "authority_granted",
        actor=actor,
        reviewer_id=reviewer_id,
        engagement_id=engagement_id,
        gate_kind=gate_kind,
    )
    return {"authority": authority}


async def revoke_authority(
    actor: str, reviewer_id: int, *, engagement_id: str, gate_kind: str
) -> dict[str, Any]:
    """Stamp `revoked_at`. The row stays, so past decisions stay explicable."""
    async with db.tool_transaction(role=_gate_role()) as conn, conn.cursor() as cur:
        await _assert_admin(cur, actor)
        await cur.execute(
            """
            UPDATE hitl.reviewer_authority SET revoked_at = now()
             WHERE reviewer_id   = %(rid)s
               AND engagement_id = %(eng)s::uuid
               AND gate_kind     = %(k)s::hitl.gate_kind
               AND revoked_at   IS NULL
            RETURNING reviewer_id, engagement_id, gate_kind::text AS gate_kind, revoked_at
            """,
            {"rid": reviewer_id, "eng": engagement_id, "k": gate_kind},
        )
        authority = await fetchone(cur)
        if authority is None:
            raise GateError(
                HTTPStatus.NOT_FOUND,
                f"reviewer {reviewer_id} holds no live {gate_kind} authority on "
                f"engagement {engagement_id}",
            )
    log.info(
        "authority_revoked",
        actor=actor,
        reviewer_id=reviewer_id,
        engagement_id=engagement_id,
        gate_kind=gate_kind,
    )
    return {"authority": authority}


async def set_admin(actor: str, reviewer_id: int, *, admin: bool) -> dict[str, Any]:
    """Grant or revoke the authority to administer this roster.

    Granting to an inactive reviewer is refused rather than silently
    ineffective: `hitl.is_reviewer_admin` requires `is_active`, so the row
    would exist and do nothing, which is the shape of bug an operator debugs
    for an hour.
    """
    async with db.tool_transaction(role=_gate_role()) as conn, conn.cursor() as cur:
        await _assert_admin(cur, actor)
        if not admin:
            await _assert_not_last_admin(cur, reviewer_id)

        await cur.execute(
            "SELECT principal, is_active FROM hitl.reviewer WHERE reviewer_id = %(rid)s",
            {"rid": reviewer_id},
        )
        target = await fetchone(cur)
        if target is None:
            raise GateError(HTTPStatus.NOT_FOUND, f"no reviewer {reviewer_id}")
        if admin and not target["is_active"]:
            raise GateError(
                HTTPStatus.CONFLICT,
                f"{target['principal']} is deactivated; reactivate them before "
                f"granting admin, or the grant will have no effect",
            )

        if admin:
            await cur.execute(
                """
                INSERT INTO hitl.reviewer_admin (reviewer_id, granted_by)
                VALUES (%(rid)s, %(by)s)
                ON CONFLICT (reviewer_id) DO UPDATE
                   SET revoked_at = NULL,
                       granted_by = EXCLUDED.granted_by,
                       granted_at = now()
                RETURNING reviewer_id, granted_by, granted_at, revoked_at
                """,
                {"rid": reviewer_id, "by": actor},
            )
        else:
            await cur.execute(
                """
                UPDATE hitl.reviewer_admin SET revoked_at = now()
                 WHERE reviewer_id = %(rid)s AND revoked_at IS NULL
                RETURNING reviewer_id, granted_by, granted_at, revoked_at
                """,
                {"rid": reviewer_id},
            )
        row = await fetchone(cur)
        if row is None and not admin:
            raise GateError(
                HTTPStatus.NOT_FOUND,
                f"{target['principal']} does not hold a live admin authority",
            )
    log.info("reviewer_admin_set", actor=actor, reviewer_id=reviewer_id, admin=admin)
    return {"admin": row}
