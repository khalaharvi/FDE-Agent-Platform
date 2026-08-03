# Adoption audit — FDE Agent Platform

Date: 2026-08-02, against v0.2.0 (first public release).
Lens: a skeptical first-time visitor — an engineer who found the repo, gives it
15 minutes, and decides whether to invest an afternoon.

**Verdict.** This is one of the most trustworthy-reading agent repos on GitHub —
tested docs, a public bug ledger, CI-asserted invariants, and an honesty section
that most projects would bury. But the adoption funnel has a hole exactly at the
moment of conviction: a new user reaches green tests and an open review console
in about ten minutes, and the console is **empty**. The one experience that
sells the platform — propose, watch deterministic gates fire, approve, merge,
see the training label — is unreachable without reverse-engineering a
1,200-line smoke test that rolls itself back. Positioning has the mirror
problem: the README explains *how it works* brilliantly before ever saying *who
it is for* or what breaks without it. Fix the aha-moment gap and two silent
quickstart breakers; the trust signals are already best-in-class and need
amplification, not repair.

---

## 1. Positioning

### What is this, and is that legible in 60 seconds?

The first sentence ("Three forward-deployed-engineering agents on Amazon
Bedrock AgentCore...") is accurate and completely opaque to anyone outside the
niche. "Forward-deployed engineering" is not a category people search for or
recognize; a visitor must infer it. The invariant blockquote that follows
("agents propose, humans dispose") is the actual hook — it is concrete,
differentiating, and lands with anyone who has watched an agent write to a
production system. It should carry more of the framing weight than the
category label does.

What the README never states:

- **The problem.** Nowhere does it say "agents that write to shared state
  without deterministic review create unauditable, untrainable systems" — the
  sentence that makes a platform lead's compliance problem click. The reader
  must assemble it from the "invariant, in depth" collapsible.
- **Who it is for.** There is no "use this if / don't use this if" section.
- **Why adopt instead of build.** The honest answer is strong: four layers of
  SQL enforcement, eight CI privilege denials, byte-identical retrieval between
  inference and RL rollout, and a review flow that emits training data — none
  of which any mainstream framework ships. It is implied, never argued.

### Adopter personas (concrete)

1. **Forward-deployed / solutions engineers** at AI labs and consultancies who
   rebuild the same engagement scaffolding per client. Hook: a repeatable
   engagement platform where the knowledge graph, gates, and drift loop
   survive between engagements. This is the category-creation audience.
2. **Platform / AI-engineering leads at enterprises** who must answer "what
   can the agent write, and who approved it?" to risk and compliance. Hook:
   the gate model — "why did this need compliance sign-off" is a `SELECT`, not
   a model output. Compliance is the door; HITL-in-SQL is the answer.
3. **OSS practitioners evaluating agent architectures**, who will arrive for
   one extractable idea — HITL gates in pure SQL, three-granularity RRF
   retrieval in SQL, drift-as-SQL, loss-mask validation — and stay if the
   repo teaches it well. They are also the advocacy engine (blog posts, HN
   comments) even when they never deploy it.

### Adopt vs. build in-house

The build-in-house engineer's default is "LangGraph interrupt + an approvals
table, how hard can it be." The repo's counterargument is real but unstated:
application-layer approval is one revoked code path away from being bypassed,
while a grant the role never had cannot be bypassed by any code path; and an
LLM-mediated review decision poisons the training labels (the model grading
itself). No README or doc says this comparatively. One paragraph — or an FAQ
entry — would arm the internal champion who has to defend the choice in a
design review.

---

## 2. Time-to-first-success

The path a new user actually walks (README quick start), with friction points
in order of encounter:

| Step | Works? | Friction |
|---|---|---|
| Install uv, `uv sync --all-packages --frozen` | Yes | None. uv manages Python itself; genuinely smooth. |
| `docker compose up -d db` | Yes | Docker is an unstated prerequisite (minor — this audience has it). |
| `./db/rebuild.sh fde` | **Fails for many** | **F1, breaker:** the script shells out to `dropdb`, `createdb`, and `psql` on the host. The compose path exists precisely for people without local Postgres — who therefore often lack the client tools too. CI knows this (`apt-get install postgresql-client`, `.github/workflows/ci.yml:66`); the README never says it. First error the user sees: `command not found: dropdb`. |
| `uv run pytest packages` | Yes | **F3:** no expected output or duration is stated. 545 tests against live Postgres take minutes; a user who skipped the `FDE_DB_DSN` export gets a quieter, mostly-skipped green run and false confidence (db-marked tests skip silently without a DSN). Say what success looks like. |
| `fde-gate-dev` → `http://127.0.0.1:8787/ui` | Starts fine | **F2, the conviction breaker:** the review queue is empty. `db/tests/smoke_test.sql` builds the full Quote-to-Cash scenario — and then `ROLLBACK`s (line 1221), by design. There is no seed data, no demo flag, and no walkthrough. The user is standing in front of the product's actual product — the gate console — with nothing to review and no signposted next step. Evaluation ends here for most people, at the exact moment it should convert. |
| "Beyond hello world" (MCP server, training, deploy) | Yes | The MCP server starts with only a DSN; tools that embed queries (`kg_search`) call Bedrock and fail without AWS credentials at call time — worth one honest sentence so nobody mistakes it for a bug. Deploy commands correctly require AWS and carry the honesty labeling. |
| Diagrams | No (on GitHub) | **F7:** the six HTML diagrams are self-contained files — invisible on github.com. Only the README's mermaid chart renders. The best visual assets in the repo are effectively unpublished. |

**Time-to-green-tests: ~10 minutes (good). Time-to-aha: unbounded (the gap).**
The smoke test proves the invariant end to end; nothing lets a human *feel* it.

---

## 3. Trust signals

### Present (unusually strong — protect these)

- CI badge, Apache-2.0 + NOTICE, SECURITY.md scoped to the invariant,
  CODE_OF_CONDUCT, issue/PR templates, committed lockfile, per-package
  READMEs for fde-mcp and fde-training.
- **Tested documentation**: `test_docs_sync.py` pins docs/06's reward table to
  `DEFAULT_WEIGHTS` and the kappa band (0.78/0.82) to the code. Almost no OSS
  project does this. It is mentioned only in CONTRIBUTING — it deserves to be
  a headline trust signal.
- **The honesty section**: explicit "not validated against live AWS" labeling,
  and a ledger of 14 real bugs with root causes (`docs/99-sources.md` §7–§8).
  This is the brand. Understated-and-verifiable beats impressive-and-vague.
- CONTRIBUTING.md is genuinely good: exact CI commands, real gotchas, "the
  ledger is a feature."

### Missing or contradicting

- **No screenshot or GIF of the review console.** It is the only UI in the
  system, the "actual product" per docs/09, and it is invisible from the
  README. For persona 2 especially, seeing the queue is believing.
- **CHANGELOG header contradicts itself**: "the project is pre-release, so
  everything currently lives under 0.1.0 and Unreleased" sits directly above
  the `[0.2.0] — 2026-08-02` first-public-release entry. Trivial, but this
  repo's brand is precision; a visitor who catches it discounts the rest.
- No public docs site (addressed by `docs-site/` in this engagement).
- No demo data or guided first merge (F2 above).
- No "who is this for / non-goals" statement.
- Repo metadata not verifiable from a local checkout — GitHub description,
  topics (`agents`, `human-in-the-loop`, `postgres`, `pgvector`, `bedrock`,
  `mcp`), and social-preview image should be checked and set; they are the
  first trust surface search and social ever see.
- No community surface (GitHub Discussions) and no `good first issue` labels
  for the persona-3 visitor who wants to engage without deploying.

---

## 4. Prioritized recommendations

Effort: S ≤ 1h, M ≤ 1 day, L = multi-day. "PINNED" = touches `README.md` or
`docs/` which `test_docs_sync.py` guards (docs/06 reward table; 0.78 and 0.82
must remain in docs/06; 0.78 must remain in README) — a human should apply
those edits and re-run `packages/fde-training/tests/test_docs_sync.py`.

### P0 — the funnel is broken without these

1. **Ship a "first proposal → review → merge" walkthrough.** Hand-written SQL
   (adapted from the smoke test's own scenario) to create one evidenced
   proposal, then approve and merge it in the local console, then query the
   sealed commit and the training label. Delivered as
   `docs-site/first-merge.mdx` in this engagement. Effort: done. Impact:
   highest available — it converts "tests pass" into "I felt the invariant."
   Audience: all three personas.
2. **Document the Postgres client-tools prerequisite** (`psql`, `createdb`,
   `dropdb`; `brew install libpq` / `apt-get install postgresql-client`) next
   to the quick start's rebuild step. Effort: S. Impact: unblocks a silent
   first-command failure. **PINNED (README)** — one added line, safe, but
   apply by hand and re-run the sync test.
3. **Add a demo-data path** so the console is not empty on first run: either
   `db/seed_demo.sql` reusing the smoke test's Quote-to-Cash inserts without
   the rollback, or a `--with-demo` flag on `rebuild.sh`. Engineering-owned
   (touches `db/`ontology conventions, not docs) — recommend, do not do from
   the marketing side. Effort: M. Impact: high; pairs with #1 so the
   walkthrough starts from something visible.

### P1 — conviction and reach

4. **Screenshot or 20-second GIF of the review console** (queue → proposal →
   approve → merge) in the README above the fold. Effort: S–M. Impact: high
   for persona 2. **PINNED (README)** — flag for hand application.
5. **Fix the stale CHANGELOG preamble** (pre-release wording above a 0.2.0
   release entry). Effort: S. Not pinned. Impact: small but brand-consistent.
6. **Add a four-line "Who this is for / not for" block to the README** naming
   the three personas and one honest non-goal (e.g., "not a general agent
   framework; if you don't need governed writes to shared state, you don't
   need this"). Effort: S. **PINNED (README)**.
7. **Publish the docs site** (`docs-site/` scaffold delivered; needs a Mintlify
   project + domain) and link it from the README header. Effort: M. Impact:
   high — it is where the category education lives.
8. **Make the six diagrams visible**: export PNG/SVG renders into the README
   or docs site, or serve the HTML via GitHub Pages. Effort: M. Impact:
   medium-high; the assets already exist and are screenshot-verified.

### P2 — advocacy and search

9. **A comparison FAQ entry** ("why not LangGraph interrupts / Temporal
   signals / an approvals microservice") making the grant-level-vs-
   application-level argument and the training-label argument. Started
   minimally in `docs-site/faq.mdx`; a fuller teaching post is future work.
   Effort: M.
10. **Set GitHub repo metadata**: description ("Agents propose, humans
    dispose — three FDE agents over a Postgres knowledge graph with
    SQL-enforced human gates"), topics, social-preview card. Effort: S.
11. **Open GitHub Discussions + label 3–5 `good first issue` items** (e.g., a
    new SoR adapter, a gate-policy example). Effort: S–M. Impact: gives
    persona 3 a participation path that is not "deploy the whole thing."
12. **Category-education content** ("what a forward-deployed engagement
    actually is; why reviewer edits are a free preference dataset") as a Learn
    section or blog series — teach the discipline, with the repo as reference
    implementation. Effort: L. Impact: compounds; this is how a non-searched
    category gets found.

---

## Appendix: what this audit deliberately did not recommend

- Rewriting the internal `docs/00–99` for outsiders. They are maintainer
  blueprints and load-bearing (sync-tested); the public docs site rewrites
  *for* outsiders instead of mutating them.
- Any metric, benchmark, testimonial, or "production-ready" language. The repo
  has zero live-AWS deployments and says so; that candor is the moat. Every
  recommendation above is compatible with the "Status and honesty" section as
  written.
