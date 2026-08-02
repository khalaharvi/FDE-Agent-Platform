-- =====================================================================
-- 015_closure_asof.sql
-- Point-in-time overloads for kg.dependency_closure and kg.impact_radius.
--
-- kg.traverse has taken p_as_of since 008:60, but the two wrappers built on
-- it did not pass it, so they always read now(). That is invisible in the
-- MCP tool surface -- a live read is what those tools promise -- and wrong
-- everywhere else: the RL rollout environment pins every episode to a
-- commit for reproducibility, and two of its eight tools were quietly
-- reading head instead. Re-running an episode against a graph that has
-- moved trains the policy on an environment that no longer exists, and
-- nothing detected it because both wrappers return perfectly plausible
-- rows.
--
-- Fixed with overloads rather than by adding a defaulted 4th parameter to
-- the existing functions: a DEFAULT would make kg.dependency_closure(a,b,c)
-- and kg.dependency_closure(a,b,c,NULL) the same function, and PostgreSQL
-- rejects the ambiguity the moment anything calls the 3-arg form. Two
-- distinct signatures also make the difference legible at every call site,
-- which is the property the parity test between rollout and MCP asserts.
-- =====================================================================

CREATE FUNCTION kg.dependency_closure(
  p_engagement uuid, p_key text, p_max_hops int, p_as_of timestamptz)
RETURNS TABLE (node_key text, node_type kg.node_type, label text,
               depth int, path text[], path_confidence real)
LANGUAGE sql STABLE PARALLEL SAFE AS $$
  SELECT t.node_key, t.node_type, t.label, t.depth, t.path, t.path_confidence
    FROM kg.traverse(p_engagement, ARRAY[p_key],
                     ARRAY['depends_on','consumes','recorded_in','gated_by']::kg.edge_type[],
                     p_max_hops, 1000, 'out', 0.0, p_as_of) t
   ORDER BY t.depth, t.path_confidence DESC;
$$;

-- The 3-arg signature is the MCP tool contract and does not change: it
-- delegates with a NULL as-of, which kg.traverse already reads as now()
-- (008:68).
CREATE OR REPLACE FUNCTION kg.dependency_closure(
  p_engagement uuid, p_key text, p_max_hops int DEFAULT 4)
RETURNS TABLE (node_key text, node_type kg.node_type, label text,
               depth int, path text[], path_confidence real)
LANGUAGE sql STABLE PARALLEL SAFE AS $$
  SELECT * FROM kg.dependency_closure(p_engagement, p_key, p_max_hops,
                                      NULL::timestamptz);
$$;

CREATE FUNCTION kg.impact_radius(
  p_engagement uuid, p_key text, p_max_hops int, p_as_of timestamptz)
RETURNS TABLE (node_key text, node_type kg.node_type, label text,
               depth int, path text[], path_confidence real)
LANGUAGE sql STABLE PARALLEL SAFE AS $$
  SELECT t.node_key, t.node_type, t.label, t.depth, t.path, t.path_confidence
    FROM kg.traverse(p_engagement, ARRAY[p_key],
                     ARRAY['depends_on','consumes','recorded_in','gated_by',
                           'precedes','hands_off_to']::kg.edge_type[],
                     p_max_hops, 1000, 'in', 0.0, p_as_of) t
   ORDER BY t.depth, t.path_confidence DESC;
$$;

CREATE OR REPLACE FUNCTION kg.impact_radius(
  p_engagement uuid, p_key text, p_max_hops int DEFAULT 4)
RETURNS TABLE (node_key text, node_type kg.node_type, label text,
               depth int, path text[], path_confidence real)
LANGUAGE sql STABLE PARALLEL SAFE AS $$
  SELECT * FROM kg.impact_radius(p_engagement, p_key, p_max_hops,
                                 NULL::timestamptz);
$$;

COMMENT ON FUNCTION kg.dependency_closure(uuid, text, int, timestamptz) IS
  'Dependency closure as of a point in valid time. Pass a commit''s '
  'sealed_at to reproduce exactly what an agent could see at that commit; '
  'the 3-arg form reads now() and is what the MCP tools call.';

COMMENT ON FUNCTION kg.impact_radius(uuid, text, int, timestamptz) IS
  'Reverse closure as of a point in valid time. Same contract as '
  'kg.dependency_closure''s 4-arg form.';

-- ---------------------------------------------------------------------
-- Grants.
--
-- No REVOKE FROM PUBLIC here, and that is deliberate rather than an
-- oversight: 008 never revoked PUBLIC EXECUTE on any kg read function, so
-- PUBLIC is how fde_gate_service and fde_ingest reach the 3-arg wrappers
-- today (neither holds an explicit kg function grant -- see 010:47-61).
-- Because the 3-arg form now delegates to the 4-arg one and SQL functions
-- run with the caller's privileges, revoking here would break those two
-- roles' existing access to a function whose contract this migration
-- promises not to change. The kg.* read surface is guarded by schema USAGE
-- and SELECT on kg tables, not by function EXECUTE; the write boundary is
-- hitl.merge_proposal, which 005 does revoke.
--
-- The explicit grants below match the four roles that hold blanket
-- `EXECUTE ON ALL FUNCTIONS IN SCHEMA kg` from 010/012. Those grants are
-- point-in-time and do not cover functions created here, so without these
-- lines each of these roles would lose the 3-arg wrappers the moment they
-- started delegating -- a grant-audit trap that costs one line to close.
-- ---------------------------------------------------------------------
GRANT EXECUTE ON FUNCTION
  kg.dependency_closure(uuid, text, int, timestamptz),
  kg.impact_radius(uuid, text, int, timestamptz)
TO fde_agent, fde_prodops, fde_training, fde_rl_rollout;
