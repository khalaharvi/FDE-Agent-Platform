---
name: update-docs
description: Bring the public docs back in sync with the code — the Mintlify site in docs-site/ and the README. Use after merging a feature or behavior change, before cutting a release, when a reviewer says the docs look stale, or when a count/flag/env var in the docs no longer matches the repo. Ends with a mandatory marketing-strategist review pass before the PR.
---

# Updating the public docs

The public docs are `docs-site/` (Mintlify, 12 pages) plus `README.md`. They are
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
git diff --stat <that-sha>..HEAD -- packages/ db/ .github/ infra/
```

Read the diff for the four things that invalidate a public page: a **count**
changing, a **command or flag** changing, an **env var** added/renamed, and a
**behavior** changing. Cosmetic refactors invalidate nothing — do not churn
pages for them.

## 2. Map what changed to the pages that claim it

| Page | What it claims | Rewrite it when this changes |
|---|---|---|
| `index` | positioning, who it's for, honest status, migration count | `README.md` framing, `db/` migration count, release state |
| `quickstart` | clone → green tests → running console, no AWS | `db/rebuild.sh`, `db/seed_demo.sql`, `pyproject.toml` deps, test/migration counts, `fde-gate-dev`, `FDE_DB_DSN` / `FDE_GATE_DEV_PRINCIPAL` |
| `first-merge` | the propose → gate → approve → merge walkthrough, run by hand | `db/seed_demo.sql` (the walkthrough uses its seeded item), `hitl.*` in `db/004`–`db/005`, the fde-gate review console UI, the eight privilege denials |
| `architecture` | five packages, DB roles, quality gates, where things run | workspace members in `pyproject.toml`, `packages/*` layout, roles in `db/010`–`db/014`, CI job list |
| `models-and-cost` | preset table, override order, cost anchors | `packages/fde-agents/src/fde_agents/common/config.py` (preset map, `DEFAULT_MODEL_ID`), `fde-agents-deploy --model-preset`, `FDE_MODEL_ID` / `FDE_MODEL_PRESET` |
| `deployment` | AgentCore runtimes, artifact shapes, CI deploy path | `fde-agents-deploy` subcommands and flags (`codezip`, `runtimes`), `infra/`, deploy workflows — **and the honesty labels below** |
| `status` | what is verified vs not, bug ledger, release state | test counts, `docs/99-sources.md` §7–§8, `CHANGELOG.md`, the version |
| `faq` | FDE definition, comparisons, no-AWS answer, cost | test count, cost figures (must equal `models-and-cost`), `CONTRIBUTING.md` |
| `concepts/human-gates` | `hitl.compute_required_gates`, four enforcement layers, four gate kinds, strict quorum | any `db/` migration touching `hitl.*`; the eight CI denial assertions |
| `concepts/knowledge-graph` | closed ontology, two time axes, hybrid retrieval, drift-as-SQL | ontology types in `db/001`–`db/002`, `kg.hybrid_search` in `db/008`, drift SQL in `db/007` |
| `concepts/agents` | three agents, the loop, 21 MCP tools | tool registrations in `packages/fde-mcp/src/fde_mcp/tools/`, agent definitions in `fde-agents` |
| `concepts/training-flywheel` | gate outcomes → SFT/preference data, the kappa band, "stop before RL" | `packages/fde-training` (`rewards.DEFAULT_WEIGHTS`, `rival_grader` kappa constants), `docs/06-training.md` |

Diagrams: `docs-site/images/*.png` are headless-Chrome exports of
`diagrams/0*.html`. Changing the source HTML means re-exporting the PNG, not
editing the image.

## 3. Re-derive every count you touch

Numbers in prose rot silently. Never copy one from another page — derive it:

```bash
ls db/[0-9]*.sql | wc -l                                          # migrations
uv run pytest packages --collect-only 2>&1 | tail -1              # test count
grep -rho 'mcp\.tool()(' packages/fde-mcp/src/fde_mcp/tools/*.py | wc -l   # MCP tools
grep -n 'smoke tests' .github/workflows/ci.yml                    # smoke count (hardcoded in the step name)
```

`--collect-only` without `-q`: root pytest `addopts` already has `-q`, and a
second one gives `-qq`, which hides the summary line you are trying to read.

The same count is usually claimed on several pages. Find every claim, then fix
them in one pass — a half-updated count is worse than a stale one, because the
two pages now contradict each other:

```bash
grep -rnE '[0-9]+ (tests|migrations|smoke tests|(MCP )?tools|packages|agents)' docs-site/ README.md
```

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
