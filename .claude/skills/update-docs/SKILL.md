---
name: update-docs
description: Bring the public docs back in sync with the code — the Mintlify site in docs-site/ and the README. Use after merging a feature or behavior change, before cutting a release, when a reviewer says the docs look stale, or when a count/flag/env var in the docs no longer matches the repo. Ends with a mandatory marketing-strategist review pass before the PR.
---

# Updating the public docs

The public docs are the Mintlify site in `docs-site/` plus `README.md`. They are
written for a stranger evaluating the project, not for a maintainer.

**Boundary — do not cross it.** `docs/00-*.md` … `docs/99-sources.md` are the
internal engineering docs. They are the *source*, never the target. If the site
and `docs/` disagree, the code decides who is wrong; you may rewrite the public
page, and you may fix an internal doc that is factually wrong, but you never
edit `docs/00–99` merely to make the public site's phrasing match. Public pages
teach outsiders; internal docs are blueprints for people who already work here.

## 1. Find the drift

```bash
git log --oneline -- docs-site/ README.md | head -5      # last docs-touching commit
git log --oneline <that-sha>..HEAD                       # what has landed since
git diff --stat <that-sha>..HEAD -- packages/ db/ .github/ infra/ \
    claude-plugin/ .claude-plugin/ \
    pyproject.toml CHANGELOG.md CONTRIBUTING.md
```

The root-level files are in that pathspec because the table below treats them as
triggers: workspace members live in `pyproject.toml`, and `status` and `faq`
claim things that only `CHANGELOG.md` and `CONTRIBUTING.md` can invalidate.
`claude-plugin/` and `.claude-plugin/` are there because the operator plugin is
a public adoption surface with its own README and its own tool count — it
landed once with the pathspec not covering it, and the whole feature was
invisible to this diff.

The last docs-touching commit is a floor, not a guarantee: it tells you what
landed since the docs last *moved*, not since they were last *correct*. A
previous pass that fixed the README and not the site leaves drift older than
that sha, which is why step 3 sweeps unconditionally instead of trusting this
diff.

Read the diff for the four things that invalidate a public page: a **count**
changing, a **command or flag** changing, an **env var** added/renamed, and a
**behavior** changing. Cosmetic refactors invalidate nothing — do not churn
pages for them.

## 2. Map what changed to the pages that claim it

| Page | What it claims | Rewrite it when this changes |
|---|---|---|
| `index` | positioning, who it's for, honest status, migration count | `README.md` framing, `db/` migration count, release state |
| `quickstart` | clone → green tests → running console, no AWS | `db/rebuild.sh`, `db/seed_demo.sql`, `pyproject.toml` deps, test/migration counts, `fde-gate-dev`, `FDE_DB_DSN` / `FDE_GATE_DEV_PRINCIPAL` |
| `first-merge` | the propose → gate → approve → merge walkthrough, run by hand | `db/seed_demo.sql` (the walkthrough uses its seeded item), `hitl.*` in `db/004`–`db/005`, the fde-gate review console UI, the twenty-five privilege denials |
| `architecture` | five packages, DB roles, quality gates, where things run | workspace members in `pyproject.toml`, `packages/*` layout, roles in `db/010`–`db/014`, CI job list |
| `models-and-cost` | preset table, override order, cost anchors | `packages/fde-agents/src/fde_agents/common/config.py` (preset map, `DEFAULT_MODEL_ID`), `fde-agents-deploy --model-preset`, `FDE_MODEL_ID` / `FDE_MODEL_PRESET` |
| `deployment` | AgentCore runtimes, artifact shapes, CI deploy path | `fde-agents-deploy` subcommands and flags (`codezip`, `runtimes`), `infra/`, deploy workflows — **and the honesty labels below** |
| `status` | what is verified vs not, bug ledger, release state | test counts, `docs/99-sources.md` §7–§8, `CHANGELOG.md`, the version |
| `faq` | FDE definition, comparisons, no-AWS answer, cost | test count, cost figures (must equal `models-and-cost`), `CONTRIBUTING.md` |
| `concepts/human-gates` | `hitl.compute_required_gates`, four enforcement layers, four gate kinds, strict quorum | any `db/` migration touching `hitl.*`; the twenty-five CI denial assertions |
| `concepts/knowledge-graph` | closed ontology, two time axes, hybrid retrieval, drift-as-SQL | ontology types in `db/001`–`db/002`, `kg.hybrid_search` in `db/008`, drift SQL in `db/007` |
| `concepts/agents` | three agents, the loop, the MCP tool count | tool registrations in `packages/fde-mcp/src/fde_mcp/tools/`, agent definitions in `fde-agents` |
| `concepts/training-flywheel` | gate outcomes → SFT/preference data, the kappa band, "stop before RL" | `packages/fde-training` (`rewards.DEFAULT_WEIGHTS`, `rival_grader` kappa constants), `docs/06-training.md` |
| `guides/for-operators` | the product-ops funnel — reviewer roster, transcript intake and its coverage number, agent launcher, review/merge, publish, playbook export — with **no terminal commands on the page** | the `/ui/*` routes in `packages/fde-gate/src/fde_gate/ui.py` (a renamed or added route breaks the walkthrough), `GET /api/workflows/{id}/playbook.md`, the `fde-operator` plugin's command list, the reviewer-roster denials |

The README is a public doc too, and it duplicates claims the site makes. Its
sections carry their own triggers:

| README section | What it claims | Rewrite it when this changes |
|---|---|---|
| the opening hook + "What is here" | positioning, who it's for, the source tree with per-directory counts | `README.md` framing decisions, `packages/*` layout, migration / tool / test counts |
| "Quick start" | the clone → green tests → console command block, no AWS | `db/rebuild.sh`, `pyproject.toml` deps, `FDE_DB_DSN` / `FDE_GATE_DEV_PRINCIPAL`, `fde-gate-dev`, the counts in its comments |
| "Choosing models (the budget dial)" | presets, the override order, the kappa floor | `packages/fde-agents/src/fde_agents/common/config.py` — **and `0.78` here is pinned by `test_docs_sync.py`** |
| "Status and honesty" | the verified / not-validated split and its evidence table | test and migration counts, the CI gates, the AWS honesty rule |

`CONTRIBUTING.md` is a public doc too — it is what a first-time contributor reads
— and it restates the same numbers:

| Doc | What it claims | Rewrite it when this changes |
|---|---|---|
| `CONTRIBUTING.md` | its own build-and-verify block, and the invariant a contributor must not break | test count, migration and smoke counts, the twenty-five privilege denials, the gate commands (`db/rebuild.sh`, `uv run pytest packages`) |
| `claude-plugin/fde-operator/README.md` | the plugin's install flow, its command and tool counts, its settings table, and the "runs as `fde_agent`, structurally cannot merge" safety claim | the MCP tool count, the commands in `claude-plugin/fde-operator/commands/`, `.mcp.json`'s env contract, `packages/fde-mcp/src/fde_mcp/config.py`'s DSN resolution order, and any grant that would weaken the safety claim — **and its verified / not-verified split, which is the AWS honesty rule applied to the plugin install** |

Keep all four consistent with each other: the site, the README,
`CONTRIBUTING.md` and the plugin README state the same counts, and the first two
state the same presets and honesty split. Fixing one and not the others is how
they start contradicting each other.

Diagrams: `docs-site/images/*.png` are headless-Chrome exports of
`diagrams/0*.html`. Changing the source HTML means re-exporting the PNG, not
editing the image.

## 3. Re-derive every count the docs claim

Not every count you touched — **every count claimed**, on every pass. Numbers rot
silently, they rot on pages your diff never pointed at, and the same count is
usually stated on three or four pages at once. Sweep first, unconditionally:

```bash
grep -rnEi '\b(one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|thirteen|fourteen|fifteen|sixteen|seventeen|eighteen|nineteen|twenty|[0-9]+)[- ]([a-z]+[- ]){0,2}(tests?|migrations?|smoke tests?|tools?|packages?|agents?|pages?|layers?|denials?|kinds?|types?|policies|axes|bugs?|documents?|diagrams?|reviewers?|tables?|images?|runtimes?|traces?|files?|invariants?|commands?|steps?|members?|statements?|gates?|checks?)' \
    docs-site/ README.md CONTRIBUTING.md CLAUDE.md claude-plugin/ docs/ .claude/agents/
```

Three things that pattern is built for:

- **Counts are spelled out as often as they are digits** — "five packages",
  "Fifteen migrations", "Fourteen real bugs". The word list runs to twenty
  because the docs use it that far up; extend it if they go higher.
- **The noun sits one to three words away from the number** — "four
  *enforcement* layers", "609 *Python* tests", "eight *gate* policies", and
  "twenty-five *CI privilege-denial* checks", which needs two. That last one is
  not hypothetical: it is the agent definition's own phrasing, and a slot of
  one word did not match it, so a stale count sat in the file that writes the
  project's marketing copy. The slot is `{0,2}` for that reason. The list holds
  base nouns (`types`, `kinds`, `axes`) rather than phrases: `types` already
  catches "15 node types" and "fifteen edge types".
- **`CONTRIBUTING.md` is in the file set**, not only the pathspec in step 1. It
  is a public doc that states the test, migration, smoke and denial counts in its
  own quickstart block, and nothing else sweeps it.
- **`docs/` and `.claude/agents/` are swept too**, even though step 1 calls
  `docs/` the *source* and never a target. The boundary is about whose prose
  wins a disagreement, not about whether numbers rot: internal blueprints rot
  exactly the same way, `docs/11-python-conventions.md` is binding on all Python
  in this repo, and an agent definition's stale count is worse than a page's
  because it reappears in everything that agent writes. Sweeping them is not
  permission to rewrite them to match the site — fix a wrong number, leave the
  framing alone.
- **Do not "fix" a dated record.** Sweeping `docs/` reaches
  `docs/superpowers/{plans,specs,audits}/` and `docs/99-sources.md` §8, which
  are point-in-time documents: a plan that says *"the '21 tools' claims are
  updated only in Task 6"*, a spec written when there were eight CI denials, a
  ledger row reading *"'13 migrations' in README | there are 12 | corrected"*.
  Every one of those numbers is **correct as history** and editing it destroys
  the record — the ledger row would stop describing the bug it exists to
  document. Read the hit, decide whether the sentence is claiming *what is true
  now* or *what was true then*, and only touch the first kind.
- **Counts appear hyphenated and singular, as adjectives** — "the *21-tool*
  loop", "a *four-layer* invariant" — which is why the noun alternatives carry
  `?` and the separator accepts `-`. That form is exactly how the last stale
  count survived a sweep that was otherwise clean.

One thing the sweep cannot catch: a count corrected in the first half of a
sentence and left stale in the second. "CI asserts twenty-five denials … if any
of the eight statements succeeds" matches on the first number and reads as a
hit you have already fixed. **Read the whole sentence around every hit**, not
the number the grep highlighted.

Extend the noun list when the docs start claiming a count it misses. Verify an
extension the way you would verify a code change: run it, and read every new hit
to confirm it is a real claim and not a coincidence.

Then derive each hit from the repo. Never copy a number from another page:

```bash
ls db/[0-9]*.sql | wc -l                                          # migrations
uv run pytest packages --collect-only 2>&1 | tail -1              # test count
grep -rho 'mcp\.tool()(' packages/fde-mcp/src/fde_mcp/tools/*.py | wc -l   # MCP tools
grep -n 'smoke tests' .github/workflows/ci.yml                    # smoke count (hardcoded in the step name)
```

`--collect-only` without `-q`: root pytest `addopts` already has `-q`, and a
second one gives `-qq`, which hides the summary line you are trying to read.

Fix every occurrence in one pass. A half-updated count is worse than a uniformly
stale one, because two pages now contradict each other and the reader cannot tell
which is current.

## 4. Authoring rules

**Mintlify.** Pages are `.mdx` with YAML frontmatter carrying `title` and
`description` (the description is the search-result and social snippet — write
it as a sentence that stands alone). Every page must be listed in
`docs-site/docs.json` navigation or `mint validate` fails. Internal links are
root-relative and extensionless (`/concepts/human-gates`, never
`./concepts/human-gates.mdx`). Components in use: `Note`, `Warning`, `Info`,
`Tip`, `Check`, `Steps`/`Step`, `Tabs`/`Tab`, `CodeGroup`, `Card`, `Columns`,
`Frame` (images live in `Frame` with a caption). Prefer a plain paragraph to a
component; callouts stop working when everything is one.

**Adding or removing a page is three edits, not one.** The `.mdx` file, an entry
in `docs-site/docs.json` navigation (`mint validate` fails without it), and a row
in the step-2 inventory table above. A page missing from that table is a page
nothing will ever check for drift — this skill goes stale the first time someone
skips that row.

**Voice.** Plain, specific, evidence-first. No hype adjectives, no "simply" or
"just", no invented metrics, users, benchmarks, or testimonials. Every concept
page ends with a runnable next step.

**AWS honesty rule.** Nothing in this repo has run against live AWS. Anything
touching Bedrock, AgentCore, Lambda, or IAM ships labeled *"written against
verified API shapes, not validated here."* `deployment` opens with that warning
and `status` splits verified from unverified — keep both intact. Do not
strengthen a claim about AWS because the code looks finished; the honesty
labeling is the project's most distinctive asset, not a disclaimer to grow out
of.

**Pinned files.** `packages/fde-training/tests/test_docs_sync.py` parses
`docs/06-training.md` and `README.md` and asserts specific numbers: the
reward-weight table must match `DEFAULT_WEIGHTS`, and the kappa band (0.78 floor
/ 0.82 suspicious-high) must appear in `docs/06`, with **0.78 present in the
README**. If you edit either file — including reflowing a table or trimming a
README section — rerun that test. Never delete 0.78 from the README to tighten
prose.

## 5. Verify

```bash
npm i -g mint                                              # once per machine
(cd docs-site && mint validate && mint broken-links)
uv sync --all-packages --frozen
uv run pytest packages/fde-training/tests/test_docs_sync.py
```

`mint validate` catches MDX that no longer builds and pages missing from
navigation; `mint broken-links` catches links to pages that moved. Both also run
in CI on PRs touching `docs-site/` (`.github/workflows/docs.yml`), which is
deliberately not a required check — read it, do not gate on it.

If a code claim changed, re-run the thing you are claiming. A quickstart that
says the tests pass is a promise; verify it against a real database
(`createdb fde && ./db/rebuild.sh fde`, then
`FDE_DB_DSN=postgresql:///fde uv run pytest packages`).

## 6. Marketing review — mandatory, not optional

**Before opening the PR, dispatch the `marketing-strategist` agent** — defined
in `.claude/agents/marketing-strategist.md`, dispatched as
`subagent_type: marketing-strategist` — to review every changed page. Give it the
list of changed files and the diff. Ask it for:

- **Positioning** — does the page still say who this is for and why they should
  care, or did the edit turn it into a changelog?
- **Adopter clarity** — can a stranger follow it without repo context? Is the
  next step still runnable?
- **Honesty compliance** — did any edit strengthen an AWS claim, invent a
  metric, or quietly drop the unverified labeling?

This step does not get skipped for small changes. A one-number fix is exactly
where positioning erodes unnoticed.

Every finding is either **applied** or **explicitly declined with a reason in
the PR body**. Silently ignoring one is not an option — a reader of the PR must
be able to see what the review said and what happened to it.

## 7. Ship it

Feature-branch workflow — `main` is protected and you cannot push to it.

```bash
git checkout -b docs/<short-topic>
git commit          # message ends: Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>
git push -u origin docs/<short-topic>
gh pr create
```

The PR body states what changed, what code change prompted it, and the
marketing-strategist findings with applied/declined status. **The user merges** —
do not merge your own docs PR.
