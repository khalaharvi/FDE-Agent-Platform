-- =====================================================================
-- 016_reviewer_admin.sql
-- Reviewer administration from the console instead of from a psql prompt.
--
-- Until now the only way to onboard a reviewer or grant them authority
-- over a gate kind was a hand-written INSERT by someone with owner rights
-- on the database. That is the one operational task the platform requires
-- and does not support, and "ask an engineer to run some SQL" is not a
-- workable answer for the people who actually clear gates.
--
-- Why `admin` is NOT a new hitl.gate_kind value
-- ----------------------------------------------
-- The obvious move -- ALTER TYPE hitl.gate_kind ADD VALUE 'admin' and
-- store the grant as another hitl.reviewer_authority row -- is wrong three
-- times over, and enum values cannot be dropped once added:
--
--   * hitl.gate_kind is the domain of hitl.gate_policy.gate_kind and
--     hitl.proposal_gate.gate_kind. A value in it is, by construction,
--     something a policy can require and a proposal can be blocked on.
--     An 'admin' gate has no meaning there: no proposal is ever waiting
--     for one, but a policy row could demand it and stall a merge forever.
--   * The enum is mirrored as a CLOSED tuple in Python
--     (fde_agents/common/models.py:74 GATE_KINDS, typed as a Literal and
--     documented as mirroring db/001 exactly) and named in the agent
--     prompts. Widening the database side alone desynchronises them
--     silently; widening both would tell the agents that 'admin' is a gate
--     kind they may reason about, which it is not.
--   * hitl.reviewer_authority is keyed (reviewer_id, engagement_id,
--     gate_kind) with engagement_id NOT NULL, but administering the
--     roster is not per-engagement. Storing it there needs a sentinel
--     engagement uuid that every query has to know about, and leaves
--     "admin on engagement X" expressible but meaningless.
--
-- So administration gets its own table. It carries the same
-- granted_by/granted_at/revoked_at shape as hitl.reviewer_authority, for
-- the same reason that table has it: who conferred a privilege and when it
-- stopped applying are audit facts, not row states to overwrite.
--
-- Deactivation, never deletion
-- -----------------------------
-- hitl.gate_decision.reviewer_id is a FK into hitl.reviewer, so a deleted
-- reviewer is a deleted decision trail. Every "remove" in this feature is
-- an UPDATE: hitl.reviewer.is_active goes false, an authority or admin row
-- gets revoked_at stamped. No role in the platform holds DELETE on any of
-- the three tables, which is what makes that a property of the schema
-- rather than a habit of the console.
-- =====================================================================

-- ---------------------------------------------------------------------
-- Who may administer the roster.
--
-- Platform-wide on purpose: the console page it gates edits hitl.reviewer
-- rows, which are global (db/004:22 -- a person is a person across every
-- engagement), so an engagement-scoped admin would be able to deactivate a
-- reviewer who only ever works somewhere else.
-- ---------------------------------------------------------------------
CREATE TABLE hitl.reviewer_admin (
  reviewer_id bigint PRIMARY KEY REFERENCES hitl.reviewer(reviewer_id),
  granted_by  text NOT NULL,
  granted_at  timestamptz NOT NULL DEFAULT now(),
  revoked_at  timestamptz
);

COMMENT ON TABLE hitl.reviewer_admin IS
  'Reviewers who may administer the roster (console: /ui/reviewers). '
  'Platform-wide, not per-engagement. Revoked by stamping revoked_at -- '
  'rows are never deleted, so "who could add reviewers last March" stays '
  'answerable.';

-- The predicate the console gates its admin page on, spelled once.
--
-- Not SECURITY DEFINER: it reads two tables the caller must already hold
-- SELECT on, so it confers nothing a role could not compute itself. The
-- point is that "is an admin" means the same thing in the page guard, in
-- every POST handler, and in the demo seed's own assertion -- three places
-- that would otherwise each spell an is_active/revoked_at join by hand.
CREATE FUNCTION hitl.is_reviewer_admin(p_principal text)
RETURNS boolean
LANGUAGE sql STABLE AS $$
  SELECT EXISTS (
    SELECT 1
      FROM hitl.reviewer r
      JOIN hitl.reviewer_admin a ON a.reviewer_id = r.reviewer_id
     WHERE r.principal    = p_principal
       AND r.is_active
       AND a.revoked_at  IS NULL
  );
$$;

COMMENT ON FUNCTION hitl.is_reviewer_admin(text) IS
  'True when this principal is an ACTIVE reviewer holding a LIVE admin '
  'grant. Deactivating a reviewer therefore also removes their admin '
  'rights, which is what "deactivate" has to mean to be worth clicking.';

-- =====================================================================
-- GRANTS
--
-- Functions arrive with EXECUTE granted to PUBLIC (013:877). Revoke
-- first, grant by name -- for a predicate that reads the roster, PUBLIC
-- would hand fde_agent and fde_rl_rollout a view of who administers the
-- platform.
-- =====================================================================
REVOKE ALL ON FUNCTION hitl.is_reviewer_admin(text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION hitl.is_reviewer_admin(text) TO fde_gate_service;

-- A new table gets no grants from 010's `ALL TABLES IN SCHEMA hitl`: that
-- expanded once, when 010 ran. So this is the whole access list for
-- hitl.reviewer_admin, and every other role -- fde_agent included -- cannot
-- so much as SELECT it.
GRANT SELECT, INSERT ON hitl.reviewer_admin TO fde_gate_service;
GRANT UPDATE (granted_by, granted_at, revoked_at) ON hitl.reviewer_admin
  TO fde_gate_service;

-- ---------------------------------------------------------------------
-- Narrowing what the gate service may rewrite on the two existing tables.
--
-- 010:48 granted fde_gate_service SELECT, INSERT, UPDATE on ALL TABLES IN
-- SCHEMA hitl, which already includes hitl.reviewer and
-- hitl.reviewer_authority -- the console could technically have written
-- them all along. What it must not have is blanket UPDATE: with it, a bug
-- in a console form could rewrite `principal` and silently re-point every
-- historical gate_decision at a different human being.
--
-- So the UPDATE is re-issued column-scoped, the same technique 011:33 uses
-- to let fde_agent triage a drift signal without resolving it and 011:78
-- uses to keep an agent away from its own training label. What is
-- deliberately NOT re-granted:
--
--   hitl.reviewer.principal      -- the identity a decision is attributed
--                                   to; changing it rewrites history
--   hitl.reviewer.display_name,
--   hitl.reviewer.email          -- editing them is not part of this
--                                   feature; add a column here when a
--                                   form for it exists, not before
--   hitl.reviewer_authority.reviewer_id / engagement_id / gate_kind
--                                -- the primary key. Moving an authority
--                                   is granting a different one; the
--                                   console INSERTs for that.
--
-- INSERT is left as 010 granted it: adding a reviewer and granting an
-- authority are both whole-row writes.
-- ---------------------------------------------------------------------
REVOKE UPDATE ON hitl.reviewer FROM fde_gate_service;
GRANT UPDATE (is_active) ON hitl.reviewer TO fde_gate_service;

REVOKE UPDATE ON hitl.reviewer_authority FROM fde_gate_service;
GRANT UPDATE (granted_by, granted_at, revoked_at) ON hitl.reviewer_authority
  TO fde_gate_service;

-- Note what is absent from this entire file: DELETE, to anyone, on any of
-- the three tables. `hitl.gate_decision.reviewer_id` references
-- `hitl.reviewer`, so deletion would either fail on the FK or take the
-- audit trail with it. Deactivation is the only removal this schema has.
